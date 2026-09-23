# RTSP → ONVIF bridge

Zet willekeurige RTSP-streams om in virtuele ONVIF-camera's. Elke virtuele camera
draait in een eigen container, krijgt een eigen MAC-adres en haalt via DHCP een
eigen IP op. Voor een NVR — UniFi Protect, Synology Surveillance Station, Frigate,
ONVIF Device Manager — is elke virtuele camera daardoor niet van een fysiek
apparaat te onderscheiden.

Beheer gaat via een web-UI op poort 8080.

```
                       ┌─ onvif-cam-a1b2  MAC 02:1f:… → DHCP 192.168.1.90 ─┐
RTSP-bronnen ──────────┼─ onvif-cam-c3d4  MAC 02:1f:… → DHCP 192.168.1.91 ─┼──→ NVR
                       └─ onvif-cam-e5f6  MAC 02:1f:… → DHCP 192.168.1.92 ─┘
                                        ▲
                       controller + web-UI (poort 8080)
```

## Wat het per camera doet

1. **DHCP** — de container hangt via macvlan aan je LAN met een vast, uit het
   camera-ID afgeleid MAC-adres, en vraagt met `udhcpc` een lease aan. Het MAC
   verandert niet als je de container opnieuw aanmaakt, dus een DHCP-reservering
   blijft geldig.
2. **RTSP-relay** — MediaMTX haalt de bronstream op zodra er een kijker is en
   publiceert hem op het eigen IP van de virtuele camera. De NVR praat dus alleen
   met de virtuele camera; het adres van de echte bron blijft onzichtbaar. Zonder
   kijkers wordt de bron niet belast.
3. **ONVIF** — een Profile-S device service op poort 80: `GetCapabilities`,
   `GetProfiles`, `GetStreamUri`, `GetSnapshotUri`, `GetDeviceInformation`,
   `GetNetworkInterfaces` en een minimale events-service. Authenticatie via
   WS-UsernameToken (digest en plaintext), HTTP Basic en HTTP Digest.
4. **WS-Discovery** — luistert op `239.255.255.250:3702` en beantwoordt Probes,
   zodat de camera opduikt in de "scan naar ONVIF-apparaten" van je NVR.
5. **Snapshots** — `/snapshot` levert een JPEG, met ffmpeg uit de stream getrokken
   en enkele seconden gecached.

## Vereisten

- **Een Linux Docker-host.** Dit is de belangrijkste randvoorwaarde: macvlan
  heeft directe toegang tot een fysieke NIC nodig. Docker Desktop op Windows of
  macOS draait in een VM met NAT — je virtuele camera's krijgen daar geen adres
  van je LAN-DHCP en zijn niet zichtbaar voor je NVR. Draai dit op een NAS,
  Proxmox-VM, Raspberry Pi of andere Linux-machine op hetzelfde netwerk.
- Een DHCP-server op dat netwerk (je router of UniFi-console).
- De host-NIC moet op hetzelfde L2-segment zitten als je NVR. Werk je met VLANs,
  gebruik dan de VLAN-subinterface als parent, bijvoorbeeld `eth0.20`.

## Installatie

```bash
git clone <deze map> rtsp-onvif-bridge
cd rtsp-onvif-bridge
cp .env.example .env
```

Zoek de juiste parent-interface op:

```bash
ip -br link            # bijv. eth0, enp3s0, ens18, of eth0.20 voor VLAN 20
```

Zet die in `.env` bij `MACVLAN_PARENT`. Daarna:

```bash
docker compose build
docker compose up -d
```

Open `http://<docker-host>:8080`. De macvlan-netwerk `camlan` wordt bij de eerste
start automatisch aangemaakt.

### Camera toevoegen

**Add camera** → naam, RTSP-URL van de bron, en de gebruikersnaam/wachtwoord
waarmee de NVR straks bij de *virtuele* camera inlogt (dat hoeft niet hetzelfde te
zijn als het wachtwoord van de echte camera — die credentials horen in de RTSP-URL
zelf, bijvoorbeeld `rtsp://admin:geheim@192.168.1.50:554/stream1`).

Optioneel een substream-URL: dat levert een tweede ONVIF-profiel op, waarmee een
NVR een lage resolutie kan kiezen voor previews.

De breedte/hoogte/framerate/bitrate zijn **alleen metadata**. Er wordt niets
gehercodeerd; ze vertellen de NVR wat hij kan verwachten. Zet ze gelijk aan de
echte stream, anders kan een NVR verkeerde keuzes maken.

Zodra de camera draait toont de kaart het MAC-adres, het via DHCP gekregen IP, het
ONVIF-endpoint en de RTSP-URL. Klik op een waarde om die te kopiëren.

### Vaste IP's

Geef in je DHCP-server een reservering op het getoonde MAC-adres. Dat MAC ligt
vast zolang de camera bestaat, ook na een rebuild of een herstart van de host.

## UniFi Protect

1. Zorg dat de Docker-host en de UniFi-console op hetzelfde netwerk zitten.
2. In Protect: **Devices → Add Devices → Third-party camera / ONVIF**. De
   virtuele camera's verschijnen in de scan; anders voeg je het getoonde IP
   handmatig toe.
3. Vul de gebruikersnaam en het wachtwoord in die je in de web-UI hebt ingesteld.

Lukt adoptie niet, zet dan tijdelijk **Require authentication** uit en probeer
opnieuw — zo zie je meteen of het probleem in de credentials zit of ergens anders.
Zet het daarna weer aan.

## De macvlan-valkuil

Een macvlan-container en zijn eigen Docker-host kunnen elkaar niet bereiken. Dat
is een kernel-eigenschap, geen bug. Voor deze opstelling maakt het meestal niets
uit: de NVR is een ander apparaat. Draait je NVR (bijvoorbeeld Frigate) wél op
dezelfde host, maak dan een macvlan-shim:

```bash
ip link add shim link eth0 type macvlan mode bridge
ip addr add 192.168.1.250/32 dev shim
ip link set shim up
ip route add 192.168.1.90/32 dev shim      # per camera-IP
```

Dit overleeft een reboot niet; zet het in een systemd-unit of in `/etc/network/interfaces`.

## Problemen oplossen

Klik **Logs** op een camerakaart — de meeste antwoorden staan daar.

| Symptoom | Oorzaak |
|---|---|
| `waiting-for-dhcp`, daarna `No DHCP lease` | Verkeerde `MACVLAN_PARENT`, of de parent hangt aan een VLAN zonder DHCP-server. Controleer met `ip -br link` en kijk of de lease-aanvraag bij je DHCP-server binnenkomt. |
| Camera is niet zichtbaar in de scan van de NVR | WS-Discovery is multicast en komt niet over VLAN-grenzen of door een router. Voeg het IP handmatig toe. |
| Adoptie faalt met een auth-fout | Test met **Require authentication** uit. Sommige NVR's sturen alleen HTTP Basic, andere alleen WS-UsernameToken — beide worden ondersteund, maar een typefout in het wachtwoord is de gebruikelijke oorzaak. |
| Beeld blijft zwart, ONVIF werkt wel | De relay krijgt de bron niet binnen. Test de bron-URL rechtstreeks: `ffplay -rtsp_transport tcp "rtsp://…"`. Probeer transport UDP als TCP hapert. |
| Snapshot geeft 503 | ffmpeg kreeg geen frame binnen 15 seconden — meestal dezelfde oorzaak als hierboven. |
| `Image not found` bij toevoegen | `docker compose build` nog niet gedraaid, of `BRIDGE_IMAGE` wijkt af van de gebouwde tag. |

Uitgebreide ONVIF-logging: zet `ONVIF_DEBUG=1` in de environment van de
camera-container (of in `docker-compose.yml`, waarna je de camera's herstart).

## Opbouw

| Pad | Rol |
|---|---|
| [app/controller/main.py](app/controller/main.py) | REST-API en web-UI |
| [app/controller/docker_mgr.py](app/controller/docker_mgr.py) | maakt het macvlan-netwerk en de camera-containers |
| [app/controller/store.py](app/controller/store.py) | configuratie in `data/cameras.json` |
| [app/camera/run.py](app/camera/run.py) | opstartvolgorde van één virtuele camera |
| [app/camera/net.py](app/camera/net.py) | DHCP op de macvlan-interface |
| [app/camera/onvif_server.py](app/camera/onvif_server.py) | ONVIF device-, media- en events-service |
| [app/camera/wsdiscovery.py](app/camera/wsdiscovery.py) | WS-Discovery responder |
| [app/camera/mediamtx.py](app/camera/mediamtx.py) | RTSP-relay |
| [app/camera/snapshot.py](app/camera/snapshot.py) | JPEG-snapshots via ffmpeg |
| [app/common/models.py](app/common/models.py) | cameramodel, MAC-generatie, validatie |

Eén image, twee rollen: `ROLE=controller` start de web-UI, `ROLE=camera` start een
virtuele camera. De controller start de camera-containers via de Docker-socket;
elke camera schrijft zijn status naar `state/<id>.json`, dat de UI uitleest.

## Beveiliging

- De controller heeft toegang tot `/var/run/docker.sock` en kan daarmee alles op
  de host. Zet de web-UI niet op het open internet.
- Camera-containers draaien met `NET_ADMIN` en `NET_RAW`; dat is het minimum voor
  een DHCP-client op een eigen interface.
- Wachtwoorden staan in platte tekst in `data/cameras.json` — de camera's moeten
  ze kunnen aanbieden. Beperk de rechten op die map.
- De RTSP-relay vraagt geen wachtwoord. Iedereen op het LAN die het IP en pad kent
  kan meekijken. Zet **Relay the stream through this camera's IP** uit als je dat
  niet wilt; dan krijgt de NVR de bron-URL rechtstreeks.
