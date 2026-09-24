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
6. **Stream-detectie** — ffprobe leest bij het opstarten de echte codec, resolutie
   en framerate van de bron, en dat is wat er via ONVIF geadverteerd wordt. Een
   H265-bron wordt dus ook als H265 aangeboden.
7. **Doorvoermeting** — de relay telt de bytes die binnenkomen en uitgaan; daaruit
   volgt de werkelijke in- en uitgaande bitrate per camera.
8. **Codec-conversie** — je kiest per camera welke codec de NVR krijgt. Wijkt de
   bron daarvan af, dan wordt er met ffmpeg omgezet; komt hij al overeen, dan
   gebeurt er niets. Welke hardware-encoder daarvoor beschikbaar is zoekt de
   bridge zelf uit.

## Vereisten

- **Een Linux Docker-host.** Dit is de belangrijkste randvoorwaarde: macvlan
  heeft directe toegang tot een fysieke NIC nodig. Docker Desktop op Windows of
  macOS draait in een VM met NAT — je virtuele camera's krijgen daar geen adres
  van je LAN-DHCP en zijn niet zichtbaar voor je NVR. Draai dit op een NAS,
  Proxmox-VM, Raspberry Pi of andere Linux-machine op hetzelfde netwerk.
- Een DHCP-server op dat netwerk (je router of UniFi-console).
- De host-NIC moet op hetzelfde L2-segment zitten als je NVR. Werk je met VLANs,
  gebruik dan de VLAN-subinterface als parent, bijvoorbeeld `eth0.20`.

## Unraid

Unraid heeft geen `git` in de basisinstallatie, dus haal de tarball op via de
web-terminal:

```bash
mkdir -p /mnt/user/appdata/rtsp-onvif-bridge
cd /mnt/user/appdata/rtsp-onvif-bridge
wget -qO- https://github.com/Lexvdpoel/rtsp-onvif-bridge/archive/refs/heads/main.tar.gz | tar xz --strip-components=1
cp .env.example .env
```

### Zonder compose-plugin (eenvoudigst)

Unraid levert `docker compose` niet mee, maar je hebt het hier ook niet nodig:
het compose-bestand definieert maar één service, de controller. De
camera-containers worden door de controller zelf aangemaakt via de Docker-socket.
Plain Docker volstaat dus:

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
  -v /mnt/user/appdata/rtsp-onvif-bridge/data:/data \
  -v /mnt/user/appdata/rtsp-onvif-bridge/state:/state \
  rtsp-onvif-bridge:latest
```

Pas `MACVLAN_PARENT` aan als je interface anders heet.

De twee `-l`-regels zorgen dat Unraid in het Docker-tabblad een camera-icoon
toont en een **WebUI**-link in het contextmenu zet, in plaats van het standaard
vraagteken zonder link. De camera-containers krijgen hetzelfde icoon; die hebben
bewust geen WebUI-link, omdat Unraids `[IP]`-placeholder naar de host wijst
terwijl een camera op zijn eigen DHCP-adres luistert.

Verschijnt het icoon niet, controleer dan eerst of de labels er werkelijk op
staan:

```bash
docker inspect onvif-bridge-controller --format '{{json .Config.Labels}}'
```

Zie je geen `net.unraid.docker.*` terug, dan is de container met een oud commando
aangemaakt. Staan ze er wel, dan kon Unraid het icoon niet ophalen; wijs het dan
naar het bestand op je server in plaats van naar een URL:

```bash
-l net.unraid.docker.icon=/mnt/user/appdata/rtsp-onvif-bridge/unraid/icon.png
```

Dat bestand staat er al na het uitpakken. Voor de camera-containers doe je
hetzelfde met `-e UNRAID_ICON=/mnt/user/appdata/rtsp-onvif-bridge/unraid/icon.png`
op de controller; die geeft het label door aan elke camera die hij aanmaakt.
Unraid cachet iconen, dus geef het na een wijziging een paar seconden en ververs
de pagina met Ctrl+F5.

### Met de Unraid-template

Wil je de container via **Add Container** beheren in plaats van via de
commandoregel, installeer dan de meegeleverde template. Poort 8080, de paden,
het icoon en de WebUI-link staan er al in:

```bash
cp /mnt/user/appdata/rtsp-onvif-bridge/unraid/rtsp-onvif-bridge.xml \
   /boot/config/plugins/dockerMan/templates-user/my-rtsp-onvif-bridge.xml
```

Daarna in Unraid: **Docker → Add Container → Template → rtsp-onvif-bridge**.
Controleer `MACVLAN_PARENT` en klik Apply.

De template verwijst naar het lokaal gebouwde image `rtsp-onvif-bridge:latest`,
dus `docker build` moet eerst gedraaid hebben. Unraid probeert bij Apply het
image te pullen; staat het al lokaal, dan is die pull-melding niet erg. Loopt het
daar vast, gebruik dan de `docker run`-route hierboven — die doet precies
hetzelfde.

### Met compose-plugin

Wil je toch compose, installeer dan **Compose Manager Plus** van *mstrhakr* uit
Community Applications. Dat is de voortzetting van de inmiddels verouderde
*Docker Compose Manager* van dcflachs en installeert de `docker compose`-CLI.

Let op: **Docker-Compose-Maker** van *grtgbln* is iets anders — dat is een web-app
om compose-bestanden mee samen te stellen, geen plugin die `docker compose`
installeert. Die heb je hier niet aan.

```bash
docker compose build
docker compose up -d
```

Twee Unraid-specifieke punten:

- **`MACVLAN_PARENT`** is op Unraid meestal `br0` (of `eth0` als bridging uit
  staat, of `br0.20` voor een VLAN). Controleer met `ip -br link`.
- **Macvlan en kernel call traces.** Unraid heeft een bekende instabiliteit met
  macvlan; de standaardaanbeveling is om Docker-netwerken op ipvlan te zetten.
  Dat kan hier niet: bij ipvlan delen alle containers het MAC-adres van de host,
  en dan krijgt elke virtuele camera geen eigen DHCP-lease meer — precies wat dit
  project moet doen. Macvlan is dus vereist. De crashes hangen samen met bridging
  op dezelfde interface; de gebruikelijke oplossing is de camera's op een
  interface te zetten waarop geen bridge actief is, bijvoorbeeld een aparte NIC of
  een VLAN-subinterface. Houd dit in de gaten bij de eerste dagen draaien.

## Installatie

```bash
git clone https://github.com/Lexvdpoel/rtsp-onvif-bridge.git
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

### Eerste keer: account aanmaken

De eerste keer dat je de UI opent vraagt hij om een gebruikersnaam en wachtwoord.
Die beveiligen deze beheerinterface — niet de camera's zelf, die hebben hun eigen
ONVIF-credentials. Het wachtwoord wordt met scrypt gehasht opgeslagen in
`data/auth.json`; de sessie is een ondertekende cookie, dus een herstart van de
container logt je niet uit.

Wachtwoord wijzigen of uitloggen kan via **Account** rechtsboven. Een
wachtwoordwijziging verloopt alle bestaande sessies.

Wachtwoord kwijt? Verwijder `data/auth.json` en herstart de controller; de UI
vraagt dan opnieuw om een account. De camera's blijven ongemoeid.

### Camera toevoegen

**Add camera** → naam, RTSP-URL van de bron, en de gebruikersnaam/wachtwoord
waarmee de NVR straks bij de *virtuele* camera inlogt (dat hoeft niet hetzelfde te
zijn als het wachtwoord van de echte camera — die credentials horen in de RTSP-URL
zelf, bijvoorbeeld `rtsp://admin:geheim@192.168.1.50:554/stream1`).

Optioneel een substream-URL: dat levert een tweede ONVIF-profiel op, waarmee een
NVR een lage resolutie kan kiezen voor previews.

De breedte/hoogte/framerate/bitrate zijn **alleen metadata**. Er wordt niets
gehercodeerd; ze vertellen de NVR wat hij kan verwachten. Standaard staat
**Detect from the source stream** aan: de camera leest die waarden bij het
opstarten met ffprobe uit de bron en adverteert wat er echt is. De ingevulde
waarden gelden dan alleen tot de probe klaar is, of als die mislukt. Zet de
detectie uit als je bewust iets anders wilt adverteren.

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

## Uitgaande codec kiezen

Per camera stel je in wat de NVR moet krijgen:

| Instelling | Wat er gebeurt |
|---|---|
| **Copy** (standaard) | De stream gaat ongewijzigd door. Geen CPU-kosten. |
| **H.264** | Is de bron al H.264, dan verandert er niets. Is hij H.265 of MJPEG, dan wordt hij omgezet. |
| **H.265 / HEVC** | Andersom hetzelfde. |

De conversie gebeurt alleen als het nodig is, en alleen zolang er iemand kijkt:
ffmpeg wordt door de relay op aanvraag gestart en stopt weer als de laatste kijker
weg is. Wat er via ONVIF geadverteerd wordt volgt de uitgaande codec, niet de
inkomende — een naar H.265 omgezette stream wordt dus ook als H.265 aangeboden.

Kan de bron niet gelezen worden bij het opstarten, dan wordt er omgezet in plaats
van gegokt: je hebt expliciet om een codec gevraagd, en die krijgen weegt zwaarder
dan de CPU die misschien niet nodig was.

### Hardware-encoding wordt zelf gevonden

De encoder staat standaard op **Automatic**. Bij de eerste start kijkt de bridge
zelf wat deze host kan en kiest dat; je hoeft niets in te vullen.

Die detectie raadt niet. Dat een encoder in ffmpeg zit, betekent nog niet dat deze
machine hem kan draaien — een oudere Intel-iGPU noemt `hevc_vaapi` wel, maar kan
H.265 alleen decoden. Daarom wordt elke kandidaat beslist met een échte
test-encode van vijf frames, in een wegwerp-container met hetzelfde ffmpeg en het
juiste device eraan. Komt die schoon terug, dan werkt hij.

Het resultaat staat in de badge **GPU:** bovenin. Klik erop om opnieuw te
detecteren, bijvoorbeeld nadat je een GPU hebt toegevoegd. Beweeg erover voor
waarom iets niet beschikbaar is.

De keuze wordt per codec gemaakt: kan je iGPU wel H.264 maar geen H.265 encoden,
dan gebruikt een H.264-camera de iGPU en valt een H.265-camera terug op software.

| Encoder | Nodig op de host | Voorkeur |
|---|---|---|
| **VA-API** | Intel- of AMD-iGPU met `/dev/dri` | eerst — breedst inzetbaar, geen limiet op het aantal gelijktijdige streams |
| **Quick Sync** | Idem, Intel-specifiek pad | tweede |
| **NVENC** | NVIDIA-GPU plus de NVIDIA container runtime | laatst — consumentenkaarten beperken het aantal gelijktijdige encode-sessies, wat gaat knellen zodra meerdere camera's tegelijk omzetten |

Wil je het zelf bepalen, kies dan een encoder uit de lijst; die keuze wordt niet
overruled. Het device wordt alleen aan de camera-container meegegeven als er
werkelijk mee geëncodeerd wordt — in Copy-modus gebeurt dat niet.

Blijft het beeld zwart na het omzetten, zet dan **Audio** op *Drop*. Niet elke
camera-audio laat zich in RTSP hermuxen, en dan faalt het hele commando.

## Doorvoer aflezen

Elke camerakaart toont de gemeten in- en uitgaande bitrate met een grafiekje van
de laatste vijf minuten; bovenaan staan de totalen over alle camera's. Beweeg met
de muis over een grafiekje voor de waarde op dat moment.

De getallen komen uit de byte-tellers van de relay, niet uit een schatting:
**in** is wat er van de bron binnenkomt, **uit** is wat er naar de NVR's gaat. Met
twee kijkers op dezelfde camera is uitgaand dus ongeveer het dubbele van inkomend.
Staat de relay uit, dan loopt het verkeer niet door de container en valt er niets
te meten.

Wordt er omgezet, dan meet **in** de stream zoals die uit ffmpeg komt, niet wat de
bron verstuurt — de relay krijgt immers het geëncodeerde resultaat binnen. De UI
zegt dat er ook bij.

**CBR of VBR** wordt afgeleid, niet uitgelezen: RTSP vertelt niet welke
rate-control de bron gebruikt. De bridge kijkt hoe sterk de gemeten bitrate
varieert over de laatste twee minuten; blijft die binnen 8% van het gemiddelde,
dan is het CBR. Het percentage staat erbij, zodat je zelf kunt zien hoe uitgesproken
het geval is.

## Backup en restore

**Backup** downloadt alle camera's als één JSON-bestand. **Restore** leest dat
bestand terug en biedt twee keuzes: alles vervangen, of de camera's uit de backup
toevoegen aan wat er al staat.

Een restore behoudt de camera-ID's, en dus de MAC-adressen. Je DHCP-reserveringen
blijven daarmee geldig, ook als je de bridge op een andere host opnieuw opbouwt.

Let op: het backupbestand bevat de ONVIF-wachtwoorden in platte tekst — anders zou
een restore geen werkende camera's opleveren. Bewaar het navenant.

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
| Beeld zwart na het kiezen van een codec | De audio van de bron laat zich niet in RTSP hermuxen. Zet **Audio** op *Drop* en herstart de camera. |
| CPU loopt vol na het kiezen van een codec | Software-encoding. Kies een hardware-encoder, of zet de codec terug op Copy als de NVR de bron-codec toch aankan. |
| Badge zegt `GPU: software only` | Er is geen werkende hardware-encoder gevonden. Beweeg over de badge voor de reden per encoder. Meestal ontbreekt `/dev/dri` op de host, of kan de iGPU de gevraagde codec niet encoden. |
| `Cannot load libva` of de encoder start niet | Je hebt handmatig een encoder gekozen die het niet doet. Zet hem op **Automatic**, of klik de GPU-badge aan om opnieuw te detecteren. |
| Bitrate blijft op nul staan | De relay staat uit voor die camera, of hij is nog niet gestart. Zonder relay loopt het verkeer niet door de container en valt er niets te meten. |
| Detectie blijft op `probing…` | ffprobe komt niet bij de bron. Kijk in de logs naar de regel die met `[probe]` begint; meestal klopt de RTSP-URL of het transport niet. |
| Wachtwoord van de UI kwijt | Verwijder `data/auth.json` en herstart de controller; hij vraagt dan opnieuw om een account. |
| `ipv4 pool is empty` bij het aanmaken van het netwerk | Het macvlan-netwerk werd zonder IPv4-pool aangemaakt. Zorg dat `MACVLAN_SUBNET` gevuld is (default `100.127.255.0/24`). De oude instelling `MACVLAN_IPAM=null` werkt niet: Docker's macvlan-driver eist een pool. |
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
| [app/controller/auth.py](app/controller/auth.py) | login, wachtwoordhashing, sessiecookies |
| [app/controller/backup.py](app/controller/backup.py) | export en validatie van een backup |
| [app/camera/probe.py](app/camera/probe.py) | ffprobe-detectie van de bronstream |
| [app/camera/stats.py](app/camera/stats.py) | doorvoermeting en CBR/VBR-afleiding |
| [app/camera/transcode.py](app/camera/transcode.py) | codec-beslissing en ffmpeg-commando |
| [app/camera/hwprobe.py](app/camera/hwprobe.py) | test-encode per hardware-encoder |
| [app/controller/hwdetect.py](app/controller/hwdetect.py) | draait de probe, cachet en kiest |
| [app/common/models.py](app/common/models.py) | cameramodel, MAC-generatie, validatie |

Eén image, twee rollen: `ROLE=controller` start de web-UI, `ROLE=camera` start een
virtuele camera. De controller start de camera-containers via de Docker-socket;
elke camera schrijft zijn status naar `state/<id>.json`, dat de UI uitleest.

## Beveiliging

- De controller heeft toegang tot `/var/run/docker.sock` en kan daarmee alles op
  de host. Zet de web-UI niet op het open internet.
- Camera-containers draaien met `NET_ADMIN` en `NET_RAW`; dat is het minimum voor
  een DHCP-client op een eigen interface.
- De web-UI zit achter een login die je bij eerste gebruik zelf instelt. Het
  beheerderswachtwoord staat scrypt-gehasht in `data/auth.json`.
- De ONVIF-wachtwoorden van de camera's staan in platte tekst in
  `data/cameras.json`, en ook in een gedownloade backup — de camera's moeten ze
  kunnen aanbieden. Beperk de rechten op die map en op je backups.
- De RTSP-relay vraagt geen wachtwoord. Iedereen op het LAN die het IP en pad kent
  kan meekijken. Zet **Relay the stream through this camera's IP** uit als je dat
  niet wilt; dan krijgt de NVR de bron-URL rechtstreeks.
