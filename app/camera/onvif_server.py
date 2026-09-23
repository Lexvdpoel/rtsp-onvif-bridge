"""A minimal but practical ONVIF Profile-S device (Device + Media + Events).

Implements the subset of the ONVIF web services that NVR clients such as UniFi
Protect, Synology Surveillance Station, Frigate and ONVIF Device Manager use to
discover a camera, list its profiles and obtain stream and snapshot URIs.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import snapshot as snapshot_mod
from .soap import (
    check_http_basic,
    check_http_digest,
    check_ws_security,
    child_text,
    envelope,
    fault,
    parse,
    utc_now,
    xml_escape,
)

MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"

# ONVIF defines these as pre-authentication operations: a client must be able to
# read them before it has working credentials.
PUBLIC_ACTIONS = {
    "GetSystemDateAndTime",
    "GetCapabilities",
    "GetServices",
    "GetServiceCapabilities",
    "GetWsdlUrl",
}


def _plus_minutes(minutes: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + minutes * 60))


def _scope_safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (value or "any"))


class OnvifService:
    """Builds ONVIF SOAP responses for one virtual camera."""

    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state  # provides .ip, .mac, .prefix

    # ------------------------------------------------------------------ helpers

    @property
    def xaddr_base(self) -> str:
        return f"http://{self.state.ip}:{self.cfg.onvif_port}"

    def device_xaddr(self) -> str:
        return f"{self.xaddr_base}/onvif/device_service"

    def media_xaddr(self) -> str:
        return f"{self.xaddr_base}/onvif/media_service"

    def events_xaddr(self) -> str:
        return f"{self.xaddr_base}/onvif/events_service"

    def profiles(self) -> list[dict]:
        cfg = self.cfg
        items = [
            {
                "token": "MainStream",
                "name": "MainStream",
                "path": "main",
                "width": cfg.width,
                "height": cfg.height,
                "fps": cfg.fps,
                "bitrate": cfg.bitrate,
                "source": cfg.source_url,
            }
        ]
        if cfg.source_url_sub:
            items.append(
                {
                    "token": "SubStream",
                    "name": "SubStream",
                    "path": "sub",
                    "width": cfg.width_sub,
                    "height": cfg.height_sub,
                    "fps": cfg.fps_sub,
                    "bitrate": cfg.bitrate_sub,
                    "source": cfg.source_url_sub,
                }
            )
        return items

    def profile_by_token(self, token: str) -> dict:
        items = self.profiles()
        for item in items:
            if item["token"] == token or token in (
                f"VEC_{item['token']}",
                f"VSC_{item['token']}",
            ):
                return item
        return items[0]

    def stream_uri(self, profile: dict) -> str:
        if self.cfg.proxy:
            return f"rtsp://{self.state.ip}:{self.cfg.rtsp_port}/{profile['path']}"
        return profile["source"]

    def snapshot_uri(self, profile: dict) -> str:
        return f"{self.xaddr_base}/snapshot?profile={profile['token']}"

    def scopes(self) -> list[str]:
        cfg = self.cfg
        return [
            "onvif://www.onvif.org/type/video_encoder",
            "onvif://www.onvif.org/type/Network_Video_Transmitter",
            "onvif://www.onvif.org/Profile/Streaming",
            f"onvif://www.onvif.org/name/{_scope_safe(cfg.name)}",
            f"onvif://www.onvif.org/hardware/{_scope_safe(cfg.model)}",
            f"onvif://www.onvif.org/location/{_scope_safe(cfg.location)}",
        ]

    # ------------------------------------------------------- response fragments

    def _video_source_config(self, profile: dict, tag: str = "tt:VideoSourceConfiguration") -> str:
        return (
            f'<{tag} token="VSC_{profile["token"]}">'
            f"<tt:Name>VSC_{profile['token']}</tt:Name>"
            "<tt:UseCount>1</tt:UseCount>"
            f"<tt:SourceToken>VS_{profile['token']}</tt:SourceToken>"
            f'<tt:Bounds x="0" y="0" width="{profile["width"]}" height="{profile["height"]}"/>'
            f"</{tag}>"
        )

    def _video_encoder_config(self, profile: dict, tag: str = "tt:VideoEncoderConfiguration") -> str:
        return (
            f'<{tag} token="VEC_{profile["token"]}">'
            f"<tt:Name>VEC_{profile['token']}</tt:Name>"
            "<tt:UseCount>1</tt:UseCount>"
            "<tt:Encoding>H264</tt:Encoding>"
            "<tt:Resolution>"
            f"<tt:Width>{profile['width']}</tt:Width>"
            f"<tt:Height>{profile['height']}</tt:Height>"
            "</tt:Resolution>"
            "<tt:Quality>5</tt:Quality>"
            "<tt:RateControl>"
            f"<tt:FrameRateLimit>{profile['fps']}</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval>"
            f"<tt:BitrateLimit>{profile['bitrate']}</tt:BitrateLimit>"
            "</tt:RateControl>"
            "<tt:H264><tt:GovLength>30</tt:GovLength>"
            "<tt:H264Profile>Main</tt:H264Profile></tt:H264>"
            "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type>"
            "<tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>"
            "<tt:Port>0</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart>"
            "</tt:Multicast>"
            "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
            f"</{tag}>"
        )

    def _profile_xml(self, profile: dict, tag: str) -> str:
        return (
            f'<{tag} token="{profile["token"]}" fixed="true">'
            f"<tt:Name>{xml_escape(profile['name'])}</tt:Name>"
            + self._video_source_config(profile)
            + self._video_encoder_config(profile)
            + f"</{tag}>"
        )

    # --------------------------------------------------------------- dispatcher

    def dispatch(self, action: str, node) -> bytes | None:
        handler = getattr(self, f"op_{action}", None)
        if handler is None:
            return None
        if action in ("GetProfiles", "GetStreamUri", "GetSnapshotUri"):
            media2 = node is not None and node.tag.startswith("{" + MEDIA2_NS + "}")
            return handler(node, media2=media2)
        return handler(node)

    # ----------------------------------------------------------- device service

    def op_GetSystemDateAndTime(self, node) -> bytes:
        def block(tag: str, tm) -> str:
            return (
                f"<tt:{tag}>"
                f"<tt:Time><tt:Hour>{tm.tm_hour}</tt:Hour>"
                f"<tt:Minute>{tm.tm_min}</tt:Minute>"
                f"<tt:Second>{tm.tm_sec}</tt:Second></tt:Time>"
                f"<tt:Date><tt:Year>{tm.tm_year}</tt:Year>"
                f"<tt:Month>{tm.tm_mon}</tt:Month>"
                f"<tt:Day>{tm.tm_mday}</tt:Day></tt:Date>"
                f"</tt:{tag}>"
            )

        return envelope(
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            "<tt:DateTimeType>NTP</tt:DateTimeType>"
            "<tt:DaylightSavings>false</tt:DaylightSavings>"
            "<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>"
            + block("UTCDateTime", time.gmtime())
            + block("LocalDateTime", time.localtime())
            + "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )

    def op_GetDeviceInformation(self, node) -> bytes:
        cfg = self.cfg
        return envelope(
            "<tds:GetDeviceInformationResponse>"
            f"<tds:Manufacturer>{xml_escape(cfg.manufacturer)}</tds:Manufacturer>"
            f"<tds:Model>{xml_escape(cfg.model)}</tds:Model>"
            f"<tds:FirmwareVersion>{xml_escape(cfg.firmware)}</tds:FirmwareVersion>"
            f"<tds:SerialNumber>{xml_escape(cfg.serial)}</tds:SerialNumber>"
            f"<tds:HardwareId>{xml_escape(cfg.model)}</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )

    def op_GetCapabilities(self, node) -> bytes:
        return envelope(
            "<tds:GetCapabilitiesResponse><tds:Capabilities>"
            "<tt:Device>"
            f"<tt:XAddr>{self.device_xaddr()}</tt:XAddr>"
            "<tt:Network><tt:IPFilter>false</tt:IPFilter>"
            "<tt:ZeroConfiguration>false</tt:ZeroConfiguration>"
            "<tt:IPVersion6>false</tt:IPVersion6>"
            "<tt:DynDNS>false</tt:DynDNS></tt:Network>"
            "<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve>"
            "<tt:DiscoveryBye>true</tt:DiscoveryBye>"
            "<tt:RemoteDiscovery>false</tt:RemoteDiscovery>"
            "<tt:SystemBackup>false</tt:SystemBackup>"
            "<tt:SystemLogging>false</tt:SystemLogging>"
            "<tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>"
            "<tt:SupportedVersions><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor>"
            "</tt:SupportedVersions></tt:System>"
            "<tt:Security><tt:TLS1.1>false</tt:TLS1.1><tt:TLS1.2>false</tt:TLS1.2>"
            "<tt:OnboardKeyGeneration>false</tt:OnboardKeyGeneration>"
            "<tt:AccessPolicyConfig>false</tt:AccessPolicyConfig>"
            "<tt:X.509Token>false</tt:X.509Token><tt:SAMLToken>false</tt:SAMLToken>"
            "<tt:KerberosToken>false</tt:KerberosToken><tt:RELToken>false</tt:RELToken>"
            "</tt:Security>"
            "</tt:Device>"
            "<tt:Events>"
            f"<tt:XAddr>{self.events_xaddr()}</tt:XAddr>"
            "<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>"
            "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
            "<tt:WSPausableSubscriptionManagerInterfaceSupport>false"
            "</tt:WSPausableSubscriptionManagerInterfaceSupport>"
            "</tt:Events>"
            "<tt:Media>"
            f"<tt:XAddr>{self.media_xaddr()}</tt:XAddr>"
            "<tt:StreamingCapabilities>"
            "<tt:RTPMulticast>false</tt:RTPMulticast>"
            "<tt:RTP_TCP>true</tt:RTP_TCP>"
            "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
            "</tt:StreamingCapabilities>"
            "</tt:Media>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )

    def op_GetServices(self, node) -> bytes:
        include_caps = child_text(node, "IncludeCapability", "false").lower() == "true"

        def service(namespace: str, xaddr: str, caps: str = "") -> str:
            body = (
                f"<tds:Service><tds:Namespace>{namespace}</tds:Namespace>"
                f"<tds:XAddr>{xaddr}</tds:XAddr>"
            )
            if include_caps and caps:
                body += f"<tds:Capabilities>{caps}</tds:Capabilities>"
            body += (
                "<tds:Version><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor>"
                "</tds:Version></tds:Service>"
            )
            return body

        return envelope(
            "<tds:GetServicesResponse>"
            + service(
                "http://www.onvif.org/ver10/device/wsdl",
                self.device_xaddr(),
                '<tds:Network IPFilter="false" ZeroConfiguration="false" '
                'IPVersion6="false" DynDNS="false"/>',
            )
            + service(
                "http://www.onvif.org/ver10/media/wsdl",
                self.media_xaddr(),
                '<trt:ProfileCapabilities MaximumNumberOfProfiles="2"/>'
                '<trt:StreamingCapabilities RTPMulticast="false" '
                'RTP_TCP="true" RTP_RTSP_TCP="true"/>',
            )
            + service(
                "http://www.onvif.org/ver10/events/wsdl",
                self.events_xaddr(),
                "<tev:WSPullPointSupport>true</tev:WSPullPointSupport>",
            )
            + "</tds:GetServicesResponse>"
        )

    def op_GetServiceCapabilities(self, node) -> bytes:
        return envelope(
            "<tds:GetServiceCapabilitiesResponse><tds:Capabilities>"
            '<tds:Network IPFilter="false" ZeroConfiguration="false" '
            'IPVersion6="false" DynDNS="false" Dot11Configuration="false" '
            'HostnameFromDHCP="true" NTP="1"/>'
            '<tds:Security TLS1.0="false" TLS1.1="false" TLS1.2="false" '
            'OnboardKeyGeneration="false" AccessPolicyConfig="false" '
            'DefaultAccessPolicy="false" Dot1X="false" RemoteUserHandling="false" '
            'X.509Token="false" SAMLToken="false" KerberosToken="false" '
            'UsernameToken="true" HttpDigest="true" RELToken="false"/>'
            '<tds:System DiscoveryResolve="false" DiscoveryBye="true" '
            'RemoteDiscovery="false" SystemBackup="false" SystemLogging="false" '
            'FirmwareUpgrade="false" HttpFirmwareUpgrade="false" '
            'HttpSystemBackup="false" HttpSystemLogging="false" '
            'HttpSupportInformation="false"/>'
            "</tds:Capabilities></tds:GetServiceCapabilitiesResponse>"
        )

    def op_GetScopes(self, node) -> bytes:
        items = "".join(
            "<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef>"
            f"<tt:ScopeItem>{scope}</tt:ScopeItem></tds:Scopes>"
            for scope in self.scopes()
        )
        return envelope(f"<tds:GetScopesResponse>{items}</tds:GetScopesResponse>")

    def op_GetHostname(self, node) -> bytes:
        return envelope(
            "<tds:GetHostnameResponse><tds:HostnameInformation>"
            "<tt:FromDHCP>true</tt:FromDHCP>"
            f"<tt:Name>{xml_escape(self.cfg.hostname)}</tt:Name>"
            "</tds:HostnameInformation></tds:GetHostnameResponse>"
        )

    def op_GetNetworkInterfaces(self, node) -> bytes:
        return envelope(
            "<tds:GetNetworkInterfacesResponse>"
            '<tds:NetworkInterfaces token="eth0">'
            "<tt:Enabled>true</tt:Enabled>"
            "<tt:Info><tt:Name>eth0</tt:Name>"
            f"<tt:HwAddress>{self.state.mac}</tt:HwAddress>"
            "<tt:MTU>1500</tt:MTU></tt:Info>"
            "<tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config>"
            "<tt:FromDHCP>"
            f"<tt:Address>{self.state.ip}</tt:Address>"
            f"<tt:PrefixLength>{self.state.prefix}</tt:PrefixLength>"
            "</tt:FromDHCP>"
            "<tt:DHCP>true</tt:DHCP>"
            "</tt:Config></tt:IPv4>"
            "</tds:NetworkInterfaces>"
            "</tds:GetNetworkInterfacesResponse>"
        )

    def op_GetUsers(self, node) -> bytes:
        return envelope(
            "<tds:GetUsersResponse><tds:User>"
            f"<tt:Username>{xml_escape(self.cfg.username)}</tt:Username>"
            "<tt:UserLevel>Administrator</tt:UserLevel>"
            "</tds:User></tds:GetUsersResponse>"
        )

    def op_GetDNS(self, node) -> bytes:
        return envelope(
            "<tds:GetDNSResponse><tds:DNSInformation>"
            "<tt:FromDHCP>true</tt:FromDHCP>"
            "</tds:DNSInformation></tds:GetDNSResponse>"
        )

    def op_GetNetworkProtocols(self, node) -> bytes:
        return envelope(
            "<tds:GetNetworkProtocolsResponse>"
            "<tds:NetworkProtocols><tt:Name>HTTP</tt:Name>"
            "<tt:Enabled>true</tt:Enabled>"
            f"<tt:Port>{self.cfg.onvif_port}</tt:Port></tds:NetworkProtocols>"
            "<tds:NetworkProtocols><tt:Name>RTSP</tt:Name>"
            "<tt:Enabled>true</tt:Enabled>"
            f"<tt:Port>{self.cfg.rtsp_port}</tt:Port></tds:NetworkProtocols>"
            "</tds:GetNetworkProtocolsResponse>"
        )

    def op_GetSystemUris(self, node) -> bytes:
        return envelope("<tds:GetSystemUrisResponse/>")

    def op_SystemReboot(self, node) -> bytes:
        return envelope(
            "<tds:SystemRebootResponse><tds:Message>Reboot is not supported"
            "</tds:Message></tds:SystemRebootResponse>"
        )

    # ------------------------------------------------------------ media service

    def op_GetProfiles(self, node, media2: bool = False) -> bytes:
        if media2:
            items = "".join(
                f'<tr2:Profiles token="{p["token"]}" fixed="true">'
                f"<tr2:Name>{xml_escape(p['name'])}</tr2:Name>"
                "<tr2:Configurations>"
                + self._video_source_config(p, "tr2:VideoSource")
                + self._video_encoder_config(p, "tr2:VideoEncoder")
                + "</tr2:Configurations></tr2:Profiles>"
                for p in self.profiles()
            )
            return envelope(
                f'<tr2:GetProfilesResponse xmlns:tr2="{MEDIA2_NS}">{items}'
                "</tr2:GetProfilesResponse>"
            )
        items = "".join(self._profile_xml(p, "trt:Profiles") for p in self.profiles())
        return envelope(f"<trt:GetProfilesResponse>{items}</trt:GetProfilesResponse>")

    def op_GetProfile(self, node) -> bytes:
        profile = self.profile_by_token(child_text(node, "ProfileToken"))
        return envelope(
            "<trt:GetProfileResponse>"
            + self._profile_xml(profile, "trt:Profile")
            + "</trt:GetProfileResponse>"
        )

    def op_GetStreamUri(self, node, media2: bool = False) -> bytes:
        uri = self.stream_uri(self.profile_by_token(child_text(node, "ProfileToken")))
        if media2:
            return envelope(
                f'<tr2:GetStreamUriResponse xmlns:tr2="{MEDIA2_NS}">'
                f"<tr2:Uri>{xml_escape(uri)}</tr2:Uri></tr2:GetStreamUriResponse>"
            )
        return envelope(
            "<trt:GetStreamUriResponse><trt:MediaUri>"
            f"<tt:Uri>{xml_escape(uri)}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT60S</tt:Timeout>"
            "</trt:MediaUri></trt:GetStreamUriResponse>"
        )

    def op_GetSnapshotUri(self, node, media2: bool = False) -> bytes:
        uri = self.snapshot_uri(self.profile_by_token(child_text(node, "ProfileToken")))
        if media2:
            return envelope(
                f'<tr2:GetSnapshotUriResponse xmlns:tr2="{MEDIA2_NS}">'
                f"<tr2:Uri>{xml_escape(uri)}</tr2:Uri></tr2:GetSnapshotUriResponse>"
            )
        return envelope(
            "<trt:GetSnapshotUriResponse><trt:MediaUri>"
            f"<tt:Uri>{xml_escape(uri)}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT60S</tt:Timeout>"
            "</trt:MediaUri></trt:GetSnapshotUriResponse>"
        )

    def op_GetVideoSources(self, node) -> bytes:
        items = "".join(
            f'<trt:VideoSources token="VS_{p["token"]}">'
            f"<tt:Framerate>{p['fps']}</tt:Framerate>"
            f'<tt:Resolution><tt:Width>{p["width"]}</tt:Width>'
            f"<tt:Height>{p['height']}</tt:Height></tt:Resolution>"
            "</trt:VideoSources>"
            for p in self.profiles()
        )
        return envelope(
            f"<trt:GetVideoSourcesResponse>{items}</trt:GetVideoSourcesResponse>"
        )

    def op_GetVideoSourceConfigurations(self, node) -> bytes:
        items = "".join(
            self._video_source_config(p, "trt:Configurations") for p in self.profiles()
        )
        return envelope(
            f"<trt:GetVideoSourceConfigurationsResponse>{items}"
            "</trt:GetVideoSourceConfigurationsResponse>"
        )

    def op_GetVideoSourceConfiguration(self, node) -> bytes:
        profile = self.profile_by_token(child_text(node, "ConfigurationToken"))
        return envelope(
            "<trt:GetVideoSourceConfigurationResponse>"
            + self._video_source_config(profile, "trt:Configuration")
            + "</trt:GetVideoSourceConfigurationResponse>"
        )

    def op_GetVideoEncoderConfigurations(self, node) -> bytes:
        items = "".join(
            self._video_encoder_config(p, "trt:Configurations") for p in self.profiles()
        )
        return envelope(
            f"<trt:GetVideoEncoderConfigurationsResponse>{items}"
            "</trt:GetVideoEncoderConfigurationsResponse>"
        )

    def op_GetVideoEncoderConfiguration(self, node) -> bytes:
        profile = self.profile_by_token(child_text(node, "ConfigurationToken"))
        return envelope(
            "<trt:GetVideoEncoderConfigurationResponse>"
            + self._video_encoder_config(profile, "trt:Configuration")
            + "</trt:GetVideoEncoderConfigurationResponse>"
        )

    def op_GetVideoEncoderConfigurationOptions(self, node) -> bytes:
        profile = self.profiles()[0]
        return envelope(
            "<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>"
            "<tt:QualityRange><tt:Min>1</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>"
            "<tt:H264>"
            f"<tt:ResolutionsAvailable><tt:Width>{profile['width']}</tt:Width>"
            f"<tt:Height>{profile['height']}</tt:Height></tt:ResolutionsAvailable>"
            "<tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>60</tt:Max></tt:GovLengthRange>"
            f"<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>{profile['fps']}</tt:Max>"
            "</tt:FrameRateRange>"
            "<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max>"
            "</tt:EncodingIntervalRange>"
            "<tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported>"
            "</tt:H264>"
            "</trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>"
        )

    def op_GetAudioSources(self, node) -> bytes:
        return envelope("<trt:GetAudioSourcesResponse/>")

    def op_GetAudioEncoderConfigurations(self, node) -> bytes:
        return envelope("<trt:GetAudioEncoderConfigurationsResponse/>")

    def op_GetAudioSourceConfigurations(self, node) -> bytes:
        return envelope("<trt:GetAudioSourceConfigurationsResponse/>")

    def op_GetMetadataConfigurations(self, node) -> bytes:
        return envelope("<trt:GetMetadataConfigurationsResponse/>")

    # ----------------------------------------------------------- events service

    def op_GetEventProperties(self, node) -> bytes:
        return envelope(
            "<tev:GetEventPropertiesResponse>"
            "<tev:TopicNamespaceLocation>"
            "http://www.onvif.org/onvif/ver10/topics/topicns.xml"
            "</tev:TopicNamespaceLocation>"
            "<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>"
            '<wstop:TopicSet xmlns:tns1="http://www.onvif.org/ver10/topics">'
            '<tns1:VideoSource><MotionAlarm wstop:topic="true">'
            '<tt:MessageDescription IsProperty="true">'
            '<tt:Source><tt:SimpleItemDescription Name="Source" '
            'Type="tt:ReferenceToken"/></tt:Source>'
            '<tt:Data><tt:SimpleItemDescription Name="State" '
            'Type="xs:boolean"/></tt:Data>'
            "</tt:MessageDescription></MotionAlarm></tns1:VideoSource>"
            "</wstop:TopicSet>"
            "<wsnt:TopicExpressionDialect>"
            "http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet"
            "</wsnt:TopicExpressionDialect>"
            "<tev:MessageContentFilterDialect>"
            "http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter"
            "</tev:MessageContentFilterDialect>"
            "</tev:GetEventPropertiesResponse>"
        )

    def op_CreatePullPointSubscription(self, node) -> bytes:
        address = f"{self.events_xaddr()}?sub={uuid.uuid4().hex[:12]}"
        return envelope(
            "<tev:CreatePullPointSubscriptionResponse>"
            "<tev:SubscriptionReference>"
            f"<wsa:Address>{address}</wsa:Address>"
            "</tev:SubscriptionReference>"
            f"<wsnt:CurrentTime>{utc_now()}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{_plus_minutes(60)}</wsnt:TerminationTime>"
            "</tev:CreatePullPointSubscriptionResponse>"
        )

    def op_PullMessages(self, node) -> bytes:
        # There is no real event source behind a plain RTSP URL, so return an
        # empty batch quickly instead of blocking for the client's full timeout.
        time.sleep(1.0)
        return envelope(
            "<tev:PullMessagesResponse>"
            f"<tev:CurrentTime>{utc_now()}</tev:CurrentTime>"
            f"<tev:TerminationTime>{_plus_minutes(60)}</tev:TerminationTime>"
            "</tev:PullMessagesResponse>"
        )

    def op_Renew(self, node) -> bytes:
        return envelope(
            "<wsnt:RenewResponse>"
            f"<wsnt:TerminationTime>{_plus_minutes(60)}</wsnt:TerminationTime>"
            f"<wsnt:CurrentTime>{utc_now()}</wsnt:CurrentTime>"
            "</wsnt:RenewResponse>"
        )

    def op_Unsubscribe(self, node) -> bytes:
        return envelope("<wsnt:UnsubscribeResponse/>")

    def op_SetSynchronizationPoint(self, node) -> bytes:
        return envelope("<tev:SetSynchronizationPointResponse/>")


class OnvifHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RTSP-ONVIF-Bridge"
    sys_version = ""

    service: OnvifService = None  # injected by serve()

    # ------------------------------------------------------------------- basics

    def log_message(self, fmt, *args):
        if os.environ.get("ONVIF_DEBUG") == "1":
            print(f"[onvif] {self.address_string()} {fmt % args}")

    def _send(self, body: bytes, status: int = 200,
              content_type: str = "application/soap+xml; charset=utf-8",
              extra_headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # --------------------------------------------------------------------- auth

    def _digest_challenge(self) -> str:
        nonce = uuid.uuid4().hex
        self.server.nonces.add(nonce)
        if len(self.server.nonces) > 256:
            self.server.nonces = set(list(self.server.nonces)[-64:])
        return f'Digest realm="onvif", qop="auth", nonce="{nonce}", opaque="{uuid.uuid4().hex}"'

    def _http_auth_ok(self) -> bool:
        cfg = self.service.cfg
        header = self.headers.get("Authorization", "")
        if not header:
            return False
        if check_http_basic(header, cfg.username, cfg.password):
            return True
        return check_http_digest(
            header, self.command, cfg.username, cfg.password, self.server.nonces
        )

    # ------------------------------------------------------------------ routing

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/snapshot", "/snapshot.jpg"):
            return self._handle_snapshot()
        if path.startswith("/onvif/"):
            return self._send(
                b"ONVIF service endpoint. Use HTTP POST with a SOAP body.",
                200, "text/plain; charset=utf-8",
            )
        if path in ("/", "/index.html"):
            cfg = self.service.cfg
            body = (
                f"{cfg.name}\n"
                f"ONVIF: {self.service.device_xaddr()}\n"
                f"RTSP:  rtsp://{self.service.state.ip}:{cfg.rtsp_port}/main\n"
            ).encode()
            return self._send(body, 200, "text/plain; charset=utf-8")
        return self._send(b"Not found", 404, "text/plain; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        try:
            action, node, root = parse(raw)
        except Exception as exc:
            return self._send(
                fault("Sender", "ter:WellFormed", f"Malformed SOAP: {exc}"), 400
            )

        if not action:
            return self._send(
                fault("Sender", "ter:OperationProhibited", "Empty SOAP body"), 400
            )

        cfg = self.service.cfg
        if cfg.require_auth and action not in PUBLIC_ACTIONS:
            present, valid = check_ws_security(root, cfg.username, cfg.password)
            if not valid and not self._http_auth_ok():
                if present:
                    return self._send(
                        fault("Sender", "ter:NotAuthorized", "Invalid credentials"), 400
                    )
                return self._send(
                    fault("Sender", "ter:NotAuthorized", "Authentication required"),
                    401,
                    extra_headers={"WWW-Authenticate": self._digest_challenge()},
                )

        try:
            response = self.service.dispatch(action, node)
        except Exception as exc:  # one bad request must not take the camera down
            print(f"[onvif] error handling {action}: {exc}")
            return self._send(fault("Receiver", "ter:Action", str(exc)), 500)

        if response is None:
            if os.environ.get("ONVIF_DEBUG") == "1":
                print(f"[onvif] unsupported operation: {action}")
            return self._send(
                fault("Sender", "ter:ActionNotSupported",
                      f"Operation '{action}' is not implemented"),
                400,
            )
        self._send(response)

    # ---------------------------------------------------------------- snapshots

    def _handle_snapshot(self):
        cfg = self.service.cfg
        if not cfg.snapshot_enabled:
            return self._send(b"Snapshots disabled", 404, "text/plain; charset=utf-8")
        if cfg.require_auth and not self._http_auth_ok():
            return self._send(
                b"Unauthorized", 401, "text/plain; charset=utf-8",
                {"WWW-Authenticate": self._digest_challenge()},
            )

        token = parse_qs(urlparse(self.path).query).get("profile", ["MainStream"])[0]
        profile = self.service.profile_by_token(token)
        source = (
            f"rtsp://127.0.0.1:{cfg.rtsp_port}/{profile['path']}"
            if cfg.proxy
            else profile["source"]
        )
        image = snapshot_mod.grab(source, cfg.rtsp_transport)
        if not image:
            return self._send(b"Snapshot unavailable", 503, "text/plain; charset=utf-8")
        self._send(image, 200, "image/jpeg", {"Cache-Control": "no-store"})


def serve(cfg, state) -> ThreadingHTTPServer:
    """Start the ONVIF HTTP server in a background thread."""
    handler = type(
        "BoundOnvifHandler", (OnvifHandler,), {"service": OnvifService(cfg, state)}
    )
    httpd = ThreadingHTTPServer(("0.0.0.0", cfg.onvif_port), handler)
    httpd.daemon_threads = True
    httpd.nonces = set()
    threading.Thread(target=httpd.serve_forever, name="onvif-http", daemon=True).start()
    print(f"[onvif] listening on 0.0.0.0:{cfg.onvif_port}")
    return httpd
