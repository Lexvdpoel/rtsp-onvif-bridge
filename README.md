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

## Mode per camera: ONVIF or UniFi Protect

Each camera decides how it presents itself.

| Mode | What it does |
|---|---|
| **ONVIF** (default) | The built-in ONVIF device this bridge serves. Works with any NVR. UniFi Protect accepts it in its reduced "generic" mode: recording works, AI detections do not. |
| **UniFi Protect** | Runs `unifi-cam-proxy`, which speaks Protect's own protocol. The camera is adopted like a real UniFi device. |

In UniFi mode the ONVIF service is not started — Protect does not use it anyway.
The RTSP relay does keep running, so your codec conversion and hardware encoding
still apply: the proxy reads from the local relay.

### Adopting a camera into Protect

1. Set the mode to **UniFi Protect**. The console address is pre-filled from the
   `UNIFI_HOST` setting; override it per camera if one talks to a different
   console.
2. Get an adoption token. Recent Protect versions no longer offer one on the
   advanced adoption screen. Sign in to the console in a browser **with your
   Ubiquiti cloud account** — a local-only account returns an authentication
   error — and then open:

   ```
   https://<console>/proxy/protect/api/cameras/manage-payload
   ```

   The response contains the token, along with the management host the camera
   will connect to:

   ```json
   {"wifi":{...},"mgmt":{"protocol":"wss","hosts":["10.0.0.1:7442"],"token":"…"}}
   ```

3. Paste the token into the form and save. It is valid for 60 minutes.

### Why a token at all

A factory UniFi camera announces itself on the LAN and Protect pushes the
adoption to it. This works the other way round: the proxy dials out to the
console and identifies itself with the token and a certificate. There is no
discovery or inform code in `unifi-cam-proxy` — the camera cannot make itself
appear in the adoption list the way a real one does, so the token is the way in.

Making it announce itself would mean reverse-engineering both the discovery
broadcast and the inbound adoption handshake. That is a separate project, not a
setting.

The token is only needed the first time. After that Protect recognises the camera
by a client certificate, which the bridge generates itself — you do not have to
extract a key from a real UniFi camera. The certificate is kept with the camera's
state in `state/certs/` so it survives a restart. Delete the camera and it is
gone, and you have to adopt again.

If you know the proxy and miss an option, **Extra proxy arguments** passes
arguments through unchanged.

`unifi-cam-proxy` is installed from a pinned commit into a virtualenv of its own,
because its dependencies are unpinned and would otherwise be resolved against
FastAPI's. That install is allowed to fail during the build: it is an opt-in
feature and upstream pulls one dependency straight from a branch archive, so a
break there should not cost you the whole image. If it did fail, the build says
so, and a camera set to UniFi mode reports it instead of failing quietly —
everything else keeps working.

### Object detection

Cameras with no intelligence of their own send pixels and nothing else, so the
detection happens here. Switch **Detect objects** on for a camera and the
container watches the **sub stream** — a few frames a second at low resolution is
enough to tell that someone is there, and it keeps the cost low enough to run
several cameras at once on a CPU. With no sub stream it falls back to the main one.

The model is SSD MobileNet v1 from the ONNX Model Zoo, trained on COCO, baked into
the image — the cameras fetch nothing at startup.

| Setting | What it does |
|---|---|
| **What to look for** | `person`, `vehicle`, `animal`, or a subset |
| **Frames per second** | how often it looks; higher reacts faster and costs more |
| **Confidence threshold** | how sure the model has to be |
| **Frames before reporting** | how many consecutive frames an object must appear in |
| **Quiet period** | how long that type stays silent afterwards |

The last two do most of the work. A small model throws out the occasional
single-frame false positive; requiring several consecutive frames removes them.
The quiet period means one person walking past is one event rather than one per
frame.

In **UniFi mode** detections are reported to Protect as smart detections. In ONVIF
mode Protect has no way to receive them, so they only appear on the camera card in
this UI.

**Packages are not supported.** COCO has no class for a parcel. Mapping "suitcase"
onto it was an option, but that is a guess dressed up as a detection. Package
detection needs a model trained specifically for it.

Being equally plain about **animal**: `unifi-cam-proxy` officially knows only
person and vehicle. Animal is passed through with the value Protect uses
internally, which has not been verified against a real console. If it is rejected,
only animal events are lost.

### What this does not do

There is no two-way audio and no PTZ. Detection runs on the sub stream and has no
zones: it reports *what* it sees, not *where* in the frame.

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
| [app/camera/detect.py](app/camera/detect.py) | object detection on the sub stream |
| [app/camera/unifi.py](app/camera/unifi.py) | certificate and invocation for unifi-cam-proxy |
| [app/camera/unifi_runner.py](app/camera/unifi_runner.py) | proxy camera that forwards detections |
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

This project stands on other people's work — MediaMTX, unifi-cam-proxy, FFmpeg,
the ONNX Model Zoo and more. [CREDITS.md](CREDITS.md) lists every component with
the licence it is distributed under, the trademark position, and the one
obligation that does not come from this repository: the FFmpeg binary inside a
built image is GPL-2.0-or-later.
