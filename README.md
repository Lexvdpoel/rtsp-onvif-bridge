# RTSP → ONVIF bridge

Turns arbitrary RTSP streams into virtual ONVIF cameras. Each virtual camera runs
in its own container, gets its own MAC address and takes its own DHCP lease. To an
NVR — UniFi Protect, Synology Surveillance Station, Frigate, ONVIF Device Manager
— each one is indistinguishable from a physical device.

Managed through a web UI on port 8080.

```
                       ┌─ onvif-cam-a1b2  MAC 02:1f:… → DHCP 192.168.1.90 ─┐
RTSP sources ──────────┼─ onvif-cam-c3d4  MAC 02:1f:… → DHCP 192.168.1.91 ─┼──→ NVR
                       └─ onvif-cam-e5f6  MAC 02:1f:… → DHCP 192.168.1.92 ─┘
                                        ▲
                       controller + web UI (port 8080)
```

## What it does per camera

1. **DHCP** — the container attaches to your LAN over macvlan with a fixed MAC
   derived from what identifies the camera, and asks for a lease with `udhcpc`.
   The MAC does not change when the container is recreated, so a DHCP reservation
   stays valid.
2. **RTSP relay** — MediaMTX pulls the source stream as soon as someone is
   watching and republishes it on the virtual camera's own IP. The NVR therefore
   only ever talks to the virtual camera; the real source's address stays hidden.
   With no viewers, the source is left alone.
3. **ONVIF** — a Profile S device service on port 80: `GetCapabilities`,
   `GetProfiles`, `GetStreamUri`, `GetSnapshotUri`, `GetDeviceInformation`,
   `GetNetworkInterfaces` and a minimal events service. Authentication over
   WS-UsernameToken (digest and plaintext), HTTP Basic and HTTP Digest.
4. **WS-Discovery** — listens on `239.255.255.250:3702` and answers probes, so the
   camera turns up in your NVR's "scan for ONVIF devices".
5. **Snapshots** — `/snapshot` serves a JPEG pulled from the stream with ffmpeg and
   cached for a few seconds.
6. **Stream detection** — ffprobe reads the real codec, resolution and frame rate
   from the source at startup, and that is what ONVIF advertises. An H.265 source
   is therefore offered as H.265.
7. **Throughput metering** — the relay counts the bytes in and out, which gives the
   real incoming and outgoing bitrate per camera.
8. **Codec conversion** — you choose per camera which codec the NVR receives. If
   the source differs, ffmpeg converts it; if it already matches, nothing happens.
   Which hardware encoder is available is worked out by the bridge itself.
9. **Object detection** — optional, per camera: people, vehicles and animals are
   detected on the sub stream, inside the container, so cameras with no
   intelligence of their own still produce events.

## Requirements

- **A Linux Docker host.** This is the main constraint: macvlan needs direct
  access to a physical NIC. Docker Desktop on Windows or macOS runs in a NATed VM
  — your virtual cameras get no address from your LAN's DHCP there and are
  invisible to your NVR. Run this on a NAS, a Proxmox VM, a Raspberry Pi or any
  other Linux machine on the same network.
- A DHCP server on that network (your router or UniFi console).
- The host NIC must be on the same L2 segment as your NVR. With VLANs, use the
  VLAN sub-interface as the parent, for example `eth0.20`.

## Unraid

Unraid has no `git` in the base install, so fetch the tarball from the web
terminal:

```bash
mkdir -p /mnt/user/appdata/rtsp-onvif-bridge
cd /mnt/user/appdata/rtsp-onvif-bridge
wget -qO- https://github.com/Lexvdpoel/rtsp-onvif-bridge/archive/refs/heads/main.tar.gz | tar xz --strip-components=1
cp .env.example .env
```

### Without the compose plugin (simplest)

Unraid does not ship `docker compose`, and you do not need it here: the compose
file defines a single service, the controller. The camera containers are created
by the controller itself through the Docker socket. Plain Docker is enough:

```bash
docker build -t rtsp-onvif-bridge:latest .

docker run -d \
  --name onvif-bridge-controller \
  --restart unless-stopped \
  -p 8080:8080 \
  -e ROLE=controller \
  -e BRIDGE_IMAGE=rtsp-onvif-bridge:latest \
  -e MACVLAN_NETWORK=camlan \
  -e MACVLAN_PARENT=br0 \
  -e STATE_DIR=/state \
  -e DATA_DIR=/data \
  -l net.unraid.docker.icon=https://raw.githubusercontent.com/Lexvdpoel/rtsp-onvif-bridge/main/unraid/icon.png \
  -l "net.unraid.docker.webui=http://[IP]:[PORT:8080]/" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /boot/config/plugins/dockerMan/templates-user:/unraid-templates \
  -v /mnt/user/appdata/rtsp-onvif-bridge/data:/data \
  -v /mnt/user/appdata/rtsp-onvif-bridge/state:/state \
  rtsp-onvif-bridge:latest
```

Change `MACVLAN_PARENT` if your interface is named differently. Paste the block in
one go: pasted line by line, a trailing `\` becomes an escaped space.

The two `-l` lines make Unraid show a camera icon in the Docker tab and put a
**WebUI** entry in the context menu, instead of the default question mark with no
link. The camera containers get the same icon; they deliberately get no WebUI
label, because Unraid's `[IP]` placeholder resolves to the host while a camera
answers on its own DHCP address.

### Still getting the question mark?

First check whether the labels actually reached the container:

```bash
docker inspect onvif-bridge-controller --format '{{json .Config.Labels}}'
```

If they are there and you still see the placeholder, it is Unraid's icon cache.
It is keyed on the *image* name, and there is a known bug: for a container without
a dockerMan template the cache is never invalidated, so a placeholder that got in
there once stays. Clear it and install the template:

```bash
# 1. drop the cached icons for this image
rm -f /boot/config/plugins/dockerMan/images/rtsp-onvif-bridge*
rm -f /usr/local/emhttp/state/plugins/dynamix.docker.manager/images/rtsp-onvif-bridge*

# 2. install the template, named after your container
mkdir -p /boot/config/plugins/dockerMan/templates-user
sed 's|<Name>rtsp-onvif-bridge</Name>|<Name>onvif-bridge-controller</Name>|' /mnt/user/appdata/rtsp-onvif-bridge/unraid/rtsp-onvif-bridge.xml > /boot/config/plugins/dockerMan/templates-user/my-onvif-bridge-controller.xml
```

Then refresh the Docker tab with Ctrl+F5.

**The filename has to match the container name.** Unraid ties a template to a
container through `my-<container name>.xml`; the label on the container alone is
not enough. If your controller runs under a different name, change both the
filename and the `<Name>` in the template. You do not have to create the container
from the template — the `docker run` command above is fine, the template only
supplies the icon and the WebUI link.

### Icons for the camera containers

Because Unraid wants a template per container, the bridge can write them itself.
Mount the templates directory into the controller:

```bash
  -v /boot/config/plugins/dockerMan/templates-user:/unraid-templates \
```

Every camera the controller creates then gets its own template with a **red**
camera icon, so you can tell them apart from the blue controller at a glance.
Delete a camera and its template goes with it.

Those templates exist only for the icon. Do not edit them through **Edit** in
Unraid: a camera needs a macvlan interface and a fixed MAC address that Unraid's
form cannot express, and Apply would recreate the container without them. Manage
the cameras from the web UI on port 8080. Leave the mount out and everything still
works; the cameras just keep the default placeholder.

### With the Unraid template

If you would rather manage the container through **Add Container** than from the
command line, install the bundled template. Port 8080, the paths, the icon and the
WebUI link are already filled in:

```bash
mkdir -p /boot/config/plugins/dockerMan/templates-user
cp /mnt/user/appdata/rtsp-onvif-bridge/unraid/rtsp-onvif-bridge.xml /boot/config/plugins/dockerMan/templates-user/my-rtsp-onvif-bridge.xml
```

Then in Unraid: **Docker → Add Container → Template → rtsp-onvif-bridge**. Check
`MACVLAN_PARENT` and click Apply.

The template references the locally built image `rtsp-onvif-bridge:latest`, so
`docker build` has to have run first. Unraid tries to pull the image on Apply; if
it is already local, that pull message is harmless. If it refuses outright, use
the `docker run` route above — it does exactly the same thing.

### With the compose plugin

If you do want compose, install **Compose Manager Plus** by *mstrhakr* from
Community Applications. That is the continuation of the now-deprecated *Docker
Compose Manager* by dcflachs, and it installs the `docker compose` CLI.

Note that **Docker-Compose-Maker** by *grtgbln* is something else — a web app for
assembling compose files, not a plugin that installs `docker compose`. It is no
use here.

```bash
docker compose build
docker compose up -d
```

Two Unraid-specific points:

- **`MACVLAN_PARENT`** is usually `br0` on Unraid (or `eth0` with bridging off, or
  `br0.20` for a VLAN). Check with `ip -br link`.
- **Macvlan and kernel call traces.** Unraid has a known instability with macvlan;
  the standard advice is to switch Docker networks to ipvlan. That is not possible
  here: with ipvlan every container shares the host's MAC address, so no virtual
  camera gets its own DHCP lease — exactly what this project exists to do. Macvlan
  is therefore required. The crashes are tied to bridging on the same interface;
  the usual fix is to put the cameras on an interface with no active bridge, such
  as a separate NIC or a VLAN sub-interface. Keep an eye on it for the first few
  days.

## Installation

```bash
git clone https://github.com/Lexvdpoel/rtsp-onvif-bridge.git
cd rtsp-onvif-bridge
cp .env.example .env
```

Find the right parent interface:

```bash
ip -br link            # e.g. eth0, enp3s0, ens18, or eth0.20 for VLAN 20
```

Put it in `.env` under `MACVLAN_PARENT`. Then:

```bash
docker compose build
docker compose up -d
```

Open `http://<docker-host>:8080`. The macvlan network `camlan` is created
automatically on first start.

### First run: create an account

The first time you open the UI it asks for a username and password. These protect
this management interface — not the cameras themselves, which have their own ONVIF
credentials. The password is stored scrypt-hashed in `data/auth.json`; the session
is a signed cookie, so restarting the container does not sign you out.

Change the password or sign out under **Account**, top right. Changing the
password invalidates every existing session.

Lost the password? Delete `data/auth.json` and restart the controller; the UI asks
for an account again. The cameras are untouched.

### Adding a camera

**Add camera** → a name, the source RTSP URL, and the username and password the
NVR will use to log in to the *virtual* camera. That does not have to match the
real camera's password — those credentials belong in the RTSP URL itself, for
example `rtsp://admin:secret@192.168.1.50:554/stream1`.

A sub stream URL is optional: it adds a second ONVIF profile, which lets an NVR
pick a low-resolution stream for previews. It is also what object detection runs
on, so it is worth filling in.

Width, height, frame rate and bitrate are **metadata only**. Nothing is
re-encoded because of them; they tell the NVR what to expect. **Detect from the
source stream** is on by default: the camera reads those values from the source
with ffprobe at startup and advertises what is really there. The values you type
then only apply until the probe finishes, or if it fails. Turn detection off if
you deliberately want to advertise something else.

Once the camera is running, the card shows its MAC address, the IP it got over
DHCP, the ONVIF endpoint and the RTSP URL. Click a value to copy it.

### Fixed IP addresses

Add a reservation in your DHCP server for the MAC address shown. That MAC is
fixed for as long as the camera exists, across rebuilds and host restarts.

The virtual MAC is derived from what *identifies* the camera, not from a random
number. Delete a camera and add it back later and it returns to the same address,
so your reservation still fits.

| What you fill in | What the MAC depends on |
|---|---|
| Nothing (the normal case) | The host and path from the source URL. Credentials are stripped, so rotating a password changes nothing. A different IP does change it. |
| **The real camera's MAC** | Only that MAC. The source may change IP, password or path and the virtual MAC stays put. |

You do not have to fill anything in. The optional MAC field is there for the case
where a camera moves to a different IP permanently. Fill it in when you add the
camera: the virtual MAC is fixed on creation and is never recalculated afterwards,
precisely so that editing a URL cannot silently invalidate a reservation.

If two cameras point at the same source, the second automatically gets a different
MAC: two NICs sharing an address on one LAN break both of them.

## RTSP transport

Two settings, one per end of the relay, and they are deliberately separate.

**Transport to the source** is how this camera reads the real one. TCP is the
safe default: it survives a router, where UDP loses packets quietly.

**Transport offered to the NVR** is what the relay hands out. Leave it on
*both* and let the NVR choose, which is what MediaMTX does on its own and what
nearly every client copes with. Narrow it only when a client picks badly:

* An NVR that chooses UDP across a routed network and shows a stalled picture
  wants **TCP only** -- with no UDP listener open, it cannot choose wrong.
* An NVR that reads TCP poorly drops and reconnects every few seconds, which
  looks like a stuttering picture. That one wants **UDP only** or *both*.

The relay's own startup line says which it ended up with:

```
INF [RTSP] listener opened on :554 (TCP), :8000 (UDP/RTP), :8001 (UDP/RTCP)
```

Multicast is never offered; nothing here reads over it.

These were one setting for a while, and that was a mistake worth recording: a
source that streams badly over UDP and an NVR that reads badly over TCP are two
different problems, and one control meant fixing either one broke the other.

## Object detection and ONVIF events

Cameras that send pixels and nothing else have no way to tell an NVR that
something happened. This bridge can watch the stream itself and publish what it
finds as ONVIF events, which is the channel an NVR already knows how to read.

Detection runs on the sub stream at a few frames a second, through the relay, so
the camera is opened once and the detector shares that connection with whatever
else is reading. The model is SSD MobileNet v1 (COCO) and distinguishes three
classes: **person**, **vehicle** and **animal**. There is no class for a parcel,
and inferring one from "suitcase" would be a guess dressed as a detection, so
package detection is absent rather than faked.

### What is published

Each finding goes out under several topic names at once. No two NVR vendors
agreed on one spelling, and publishing an event four ways costs nothing while
covering far more products than picking a favourite:

| Topic | Carries | Who reads it |
|---|---|---|
| `tns1:VideoSource/MotionAlarm` | `State` true/false | almost everything |
| `tns1:RuleEngine/CellMotionDetector/Motion` | `IsMotion` true/false | the ONVIF Profile S standard for motion |
| `tns1:RuleEngine/ObjectDetector/Object` | `ObjectType` Human/Vehicle/Animal, `Likelihood` | clients that want to know *what* it was |
| `tns1:RuleEngine/MyRuleDetector/PeopleDetect` (and `VehicleDetect`, `AnimalDetect`) | `State` true/false | clients written against Hikvision cameras |

Nothing the model cannot see is advertised. There is no tamper detection, no
line crossing, no face recognition and no licence plate reading, so no filter in
your NVR is left waiting on an event that will never arrive.

### Motion is a state, not a ping

Motion turns on when something is in view and off again once nothing has been
seen for the **motion hold** period. That matters more than it sounds: an NVR
that only ever receives "motion started" shows a camera that has been moving
since the day it was plugged in.

Two timers, and they do different jobs:

* **Motion hold** (default 8s) — how long motion stays on after the last frame
  that saw something. Raise it if your NVR shows motion flickering off between
  the frames of a slow walk.
* **Quiet period** (default 30s) — how long before the *same class* is reported
  as a new smart event. This one only affects the object events; motion is
  driven by every analysed frame, so a person standing still keeps motion on
  without generating an event every second.

### How an NVR receives them

Over a PullPoint subscription, which is what ONVIF clients use in practice: the
NVR calls `CreatePullPointSubscription`, gets back an address of its own, and
long-polls it with `PullMessages`. Each subscription has its own queue, so two
systems can watch the same camera without stealing each other's events.
`Renew` and `Unsubscribe` work; a subscription nothing renews is dropped after
ten minutes so a vanished NVR stops costing memory.

The camera card shows how many subscribers it has. If detections appear there
but nothing reaches your NVR, that number is the first thing to look at — "no
NVR subscribed" means the problem is on the other side.

### A note on UniFi Protect

Protect added motion support for third-party ONVIF cameras in application
**7.1.55**, and it is reported working in **7.1.60**. On older versions Protect
does not subscribe to a third-party camera's events at all, so nothing here will
reach it however correctly it is published. Check your Protect version before
concluding the camera is at fault.

Whether Protect acts on the *smart* topics — person, vehicle, animal — rather
than treating everything as plain motion is not something this project can
confirm. The motion topics are the ones with a working precedent.

### The three views

The header switches between them.

**Cameras** is the list: one card per camera, with its addresses, throughput and
controls.

**Live** is a grid of every running camera, sized to the window -- the column
count follows the width, and the tile size decides how many fit. Two things to
know about the quality setting, because it costs real CPU:

* **Low** reads each camera's sub stream. One small encoder per tile.
* **High** reads the main stream. One full-resolution encoder per tile, so a
  grid of eight is eight of them on the host at once.

The streams stop when you leave the view, and when the tab goes to the
background. That is not tidiness: a tile left connected is an encoder left
running, and nothing in the interface would show it.

Video reaches the browser as MJPEG. HLS would need a JavaScript player, since
only Safari plays it natively, and this page deliberately loads nothing from the
internet so it works on a machine that has none. WebRTC would need signalling
and a spread of UDP ports. An `<img>` pointed at a multipart stream needs
neither and works everywhere — at the cost of bandwidth, which is why the frame
rate is a handful per second rather than the full stream.

<a id="live_view"></a>

#### How the frames get there

The browser only ever talks to the controller. Nothing on the page reaches a
camera directly, which also happens to be the only arrangement that works: the
cameras are on a **macvlan** network, and a container on Docker's ordinary
bridge cannot route to macvlan children on the same host — traffic leaves by the
physical interface and never comes back. The address looks perfectly reachable,
which is what makes it confusing.

So the two do not talk over the network at all. They talk through the volume
they already share:

1. A browser opens a tile. The controller writes `state/live/<id>.want` saying
   which quality, frame rate and width, and keeps rewriting it while the tile is
   open.
2. The camera notices within a second, starts one encoder, and writes each frame
   to `state/live/<id>.jpg` — to a temporary name and then moved into place, so
   the controller reads a whole picture or the previous one, never half of each.
3. The controller reads those frames and hands them to the browser as multipart
   JPEG.
4. A few seconds after the last request, the camera stops encoding and removes
   the frame.

Nothing encodes while nobody is watching. That matters on a host already
relaying three streams and running three detectors: an always-on encoder per
camera would be a permanent cost for an occasional look.

**If a tile says "no picture"**, the camera's own log says why — it is the one
running ffmpeg:

```
[live] someone is watching: low stream at 4 fps, 640px
[live] no frames from rtsp://127.0.0.1:554/sub: <what ffmpeg said>
```

**Timeline** is every camera's detections on one day, on a single track.

Marks that would land on top of each other are drawn as one, a little thicker,
split into coloured segments when the group holds more than one kind — so a
mixed group reads as mixed before you touch it. Hover to see what is inside:
time, camera and class, up to six of them and a count for the rest. Click to
open the whole group side by side, which is the point of grouping them rather
than hiding them.

Grouping is bounded as well as near: detections keep joining while each is close
to the last, but a group closes once it covers about half an hour. Without that
second rule, a camera that sees something every few minutes all afternoon would
chain into one mark covering the afternoon.

The per-camera timeline behind the **Detections** button works the same way, and
clicking a still opens the group it belongs to rather than that still alone.

### The detection timeline

Every detection saves a still, and the **Detections** button on a camera card
opens them on a timeline: a 24-hour strip with one mark per detection, coloured
by class, and the stills themselves underneath. Click a mark to jump to its
still, or a still to enlarge it. Pick another day from the list.

The point of the stills is not evidence, it is judgement. A count tells you the
detector fired; a still tells you whether it fired at a person or at a bin bag
moving in the wind, which is what decides whether the threshold is set anywhere
near right.

Turn it off per camera with **Save a still per detection** if you only want the
events.

### Disk, and how it is kept in hand

Under **Settings**, one budget covers every camera. The default is **10 GB**,
which at roughly 50 KB a still is around two hundred thousand of them — months
for a quiet camera, days for a busy one.

It is a ceiling rather than an estimate. A sweep runs every two minutes, and
whenever the budget changes, deleting the oldest stills until the store fits.
Lowering the budget frees the space immediately.

Oldest-first applies **across all cameras, not per camera**. A budget divided
per camera would have a busy driveway discarding this morning while a quiet back
garden still held last month, which is the wrong trade for anyone who has to go
looking for something. Age decides, wherever the still came from.

The stills live in the shared state volume, alongside the cameras' own files:

```
<state>/events/<camera id>/<YYYY-MM-DD>/<epoch ms>-<type>-<score>.jpg
```

Everything about an event is in its path, so there is no index to fall out of
step with the files. Delete a file and the event is gone; copy one in and it
appears.

On Unraid that volume is usually `/mnt/user/appdata/rtsp-onvif-bridge/state`,
which commonly sits on the cache SSD. **Ten gigabytes of stills will land
there.** If that is not where you want them, either lower the budget or mount
`/state` somewhere with more room.

Deleting a camera deletes its stills with it.

### Tuning it

With `DETECT_DEBUG=1` on the controller, every camera logs the candidates it
discarded alongside the threshold they were measured against:

```
[detect] candidates: vehicle 0.31, person 0.18 | reporting at >= 0.50 after 3 frames in a row
```

Without that there is no way to tell a detector that saw nothing from one that
saw the car at 0.31 and threw it away — and those two call for opposite fixes.
Small or distant objects are the hard case: the model works on a 300x300 image,
so a car at the far end of a yard is a handful of pixels whatever the source
resolution.


## Choosing the outgoing codec

Per camera you set what the NVR receives:

| Setting | What happens |
|---|---|
| **Copy** (default) | The stream passes through unchanged. No CPU cost. |
| **H.264** | If the source is already H.264, nothing changes. If it is H.265 or MJPEG, it is converted. |
| **H.265 / HEVC** | The same the other way round. |

Conversion only happens when it is needed, and only while someone is watching:
ffmpeg is started on demand by the relay and stops again when the last viewer
leaves. What ONVIF advertises follows the outgoing codec, not the incoming one, so
a stream converted to H.265 is offered as H.265.

If the source cannot be read at startup, it is converted rather than guessed: you
asked for a specific codec, and delivering it weighs more than the CPU that might
not have been needed.

### Hardware encoding is detected for you

The encoder defaults to **Automatic**. On first start the bridge works out what
this host can do and picks accordingly; you do not have to fill anything in.

That detection does not guess. An encoder being present in ffmpeg says nothing
about whether this machine can run it — an older Intel iGPU lists `hevc_vaapi` but
can only decode H.265. So every candidate is settled by a real test encode of five
frames, in a throwaway container with the same ffmpeg and the right device
attached. If that returns cleanly, it works.

The result is in the **GPU:** badge at the top. Click it to detect again, for
instance after adding a GPU. Hover it for the reason something is unavailable.

The choice is made per codec: if your iGPU can encode H.264 but not H.265, an
H.264 camera uses the iGPU and an H.265 camera falls back to software.

| Encoder | Needed on the host | Preference |
|---|---|---|
| **VA-API** | Intel or AMD iGPU with `/dev/dri` | first — broadest, and no limit on concurrent streams |
| **Quick Sync** | the same hardware, Intel-specific path | second |
| **NVENC** | NVIDIA GPU plus the NVIDIA container runtime | last — consumer cards cap concurrent encode sessions, which starts to bite once several cameras convert at once |

To decide for yourself, pick an encoder from the list; that choice is not
overridden. The device is only passed into the camera container when something is
actually encoded with it — in Copy mode it is not.

If the picture stays black after switching codec, set **Audio** to *Drop*. Not
every camera's audio can be remuxed into RTSP, and then the whole command fails.

## Reading throughput

Each camera card shows the measured incoming and outgoing bitrate with a graph of
the last five minutes; the totals across all cameras are at the top. Hover a graph
for the value at that point.

The numbers come from the relay's byte counters, not from an estimate: **in** is
what arrives from the source, **out** is what goes to the NVRs. With two viewers on
one camera, outgoing is roughly double incoming. With the relay off, the traffic
does not pass through the container and there is nothing to measure.

When a stream is being converted, **in** measures what comes out of ffmpeg rather
than what the source sends — the relay receives the encoded result. The UI says so.

**CBR or VBR** is inferred, not read: RTSP does not report which rate control the
source uses. The bridge looks at how much the measured bitrate varies over the last
two minutes; within 8% of the mean it calls it CBR. The percentage is shown so you
can judge how clear-cut the case is.

## Backup and restore

**Backup** downloads every camera as one JSON file. **Restore** reads it back and
offers two choices: replace everything, or add the cameras from the backup to what
is already there.

A restore preserves the camera ids, and therefore the MAC addresses, so your DHCP
reservations remain valid even if you rebuild the bridge on another host.

Note that the backup file contains the ONVIF passwords in plain text — otherwise a
restore would not produce working cameras. Store it accordingly.

## The macvlan catch

A macvlan container and its own Docker host cannot reach each other. That is a
kernel property, not a bug. For this setup it usually does not matter: the NVR is
a different machine. If your NVR (Frigate, say) runs on the same host, add a
macvlan shim:

```bash
ip link add shim link eth0 type macvlan mode bridge
ip addr add 192.168.1.250/32 dev shim
ip link set shim up
ip route add 192.168.1.90/32 dev shim      # one per camera IP
```

This does not survive a reboot; put it in a systemd unit or in
`/etc/network/interfaces`.

## Troubleshooting

Click **Logs** on a camera card — most answers are there.

| Symptom | Cause |
|---|---|
| The camera answers probes but never appears in Protect | Check what the log says it answered. If nothing is answered at all, UDP 10001 is not reaching the camera. If it answers and Protect still shows nothing, confirm the reply is well formed from another machine: `nmap -sU -p 10001 --script ubiquiti-discovery <camera ip>`. A camera that shows up there but not in Protect is being rejected by the console, not by the network. |
| `waiting-for-dhcp`, then `No DHCP lease` | Wrong `MACVLAN_PARENT`, or the parent sits on a VLAN with no DHCP server. Check with `ip -br link` and see whether the request reaches your DHCP server. |
| The camera does not show up in the NVR's scan | WS-Discovery is multicast and does not cross VLAN boundaries or routers. Add the IP by hand. |
| Adoption fails with an auth error | Test with **Require authentication** off. Some NVRs send only HTTP Basic, others only WS-UsernameToken — both are supported, but a typo in the password is the usual cause. |
| Picture stays black, ONVIF works | The relay cannot reach the source. Test the source URL directly: `ffplay -rtsp_transport tcp "rtsp://…"`. Try UDP transport if TCP stalls. |
| Snapshot returns 503 | ffmpeg got no frame within 15 seconds — usually the same cause as above. |
| Picture black after choosing a codec | The source's audio cannot be remuxed into RTSP. Set **Audio** to *Drop* and restart the camera. |
| CPU saturated after choosing a codec | Software encoding. Pick a hardware encoder, or set the codec back to Copy if the NVR can handle the source codec anyway. |
| Badge says `GPU: software only` | No working hardware encoder was found. Hover the badge for the reason per encoder. Usually `/dev/dri` is missing on the host, or the iGPU cannot encode the codec asked for. |
| `Cannot load libva`, or the encoder will not start | You picked an encoder by hand that does not work. Set it to **Automatic**, or click the GPU badge to detect again. |
| Bitrate stays at zero | The relay is off for that camera, or it has not started. Without the relay the traffic does not pass through the container. |
| Stream detection stuck on `probing…` | ffprobe cannot reach the source. Look for the `[probe]` line in the logs; usually the RTSP URL or the transport is wrong. |
| Lost the UI password | Delete `data/auth.json` and restart the controller; it asks for an account again. |
| `ipv4 pool is empty` when creating the network | The macvlan network was created without an IPv4 pool. Make sure `MACVLAN_SUBNET` is set (default `100.127.255.0/24`). The old `MACVLAN_IPAM=null` setting does not work: Docker's macvlan driver requires a pool. |
| `Image not found` when adding a camera | `docker compose build` has not run, or `BRIDGE_IMAGE` differs from the tag you built. |

For verbose ONVIF logging, set `ONVIF_DEBUG=1` in the camera container's
environment (or in `docker-compose.yml`, then restart the cameras).

## Layout

| Path | Role |
|---|---|
| [app/controller/main.py](app/controller/main.py) | REST API and web UI |
| [app/controller/docker_mgr.py](app/controller/docker_mgr.py) | creates the macvlan network and the camera containers |
| [app/controller/store.py](app/controller/store.py) | configuration in `data/cameras.json` |
| [app/controller/auth.py](app/controller/auth.py) | login, password hashing, session cookies |
| [app/controller/backup.py](app/controller/backup.py) | export and validation of a backup |
| [app/controller/hwdetect.py](app/controller/hwdetect.py) | runs the hardware probe, caches and chooses |
| [app/camera/run.py](app/camera/run.py) | boot order of a single virtual camera |
| [app/camera/net.py](app/camera/net.py) | DHCP on the macvlan interface |
| [app/camera/onvif_server.py](app/camera/onvif_server.py) | ONVIF device, media and events services |
| [app/camera/wsdiscovery.py](app/camera/wsdiscovery.py) | WS-Discovery responder |
| [app/camera/mediamtx.py](app/camera/mediamtx.py) | RTSP relay |
| [app/camera/snapshot.py](app/camera/snapshot.py) | JPEG snapshots via ffmpeg |
| [app/camera/probe.py](app/camera/probe.py) | ffprobe detection of the source stream |
| [app/camera/stats.py](app/camera/stats.py) | throughput metering and CBR/VBR inference |
| [app/camera/transcode.py](app/camera/transcode.py) | codec decision and the ffmpeg command |
| [app/camera/hwprobe.py](app/camera/hwprobe.py) | test encode per hardware encoder |
| [app/camera/events.py](app/camera/events.py) | ONVIF topics, subscriptions and the PullPoint queue |
| [app/camera/recorder.py](app/camera/recorder.py) | saves a still per detection, off the main stream |
| [app/common/clips.py](app/common/clips.py) | where stills live, and how their names carry their metadata |
| [app/controller/clip_store.py](app/controller/clip_store.py) | lists and serves stills, and prunes them to the budget |
| [app/controller/settings.py](app/controller/settings.py) | settings that apply to the whole bridge |
| [app/common/mjpeg.py](app/common/mjpeg.py) | the live view: the request handshake, the encoder and the multipart framing |
| [app/camera/live.py](app/camera/live.py) | writes live frames into the shared volume while someone watches |
| [app/camera/detect.py](app/camera/detect.py) | object detection on the sub stream |
| [app/common/models.py](app/common/models.py) | camera model, MAC generation, validation |
| [tools/check_ui.py](tools/check_ui.py) | checks the web UI's inline script |
| [unraid/](unraid/) | Unraid template and icons |

One image, two roles: `ROLE=controller` starts the web UI, `ROLE=camera` starts a
virtual camera. The controller starts the camera containers through the Docker
socket; each camera writes its status to `state/<id>.json`, which the UI reads.

## Security

- The controller has access to `/var/run/docker.sock` and can therefore do
  anything on the host. Do not expose the web UI to the open internet.
- Camera containers run with `NET_ADMIN` and `NET_RAW`, the minimum for a DHCP
  client on its own interface.
- The web UI sits behind a login you set on first use. The admin password is
  stored scrypt-hashed in `data/auth.json`.
- The cameras' ONVIF passwords are stored in plain text in `data/cameras.json`,
  and in a downloaded backup — the cameras have to be able to present them.
  Restrict permissions on that directory and on your backups.
- The RTSP relay asks for no password. Anyone on the LAN who knows the IP and path
  can watch. Turn **Relay the stream through this camera's IP** off if you do not
  want that; the NVR then gets the source URL directly.

## Licence

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Use it, change
it, build on it, commercially or otherwise; keep the licence and the attribution
to this project, and mark the files you changed.

## Credits

This project stands on other people's work — MediaMTX, FFmpeg,
the ONNX Model Zoo and more. [CREDITS.md](CREDITS.md) lists every component with
the licence it is distributed under, the trademark position, and the one
obligation that does not come from this repository: the FFmpeg binary inside a
built image is GPL-2.0-or-later.
