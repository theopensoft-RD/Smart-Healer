"""The ordered registry. Run order is least-invasive / dependency-first:
boot (marker only) -> dependency (redis) -> liveness -> radar -> loop (report) -> dropler (report)
-> stream-camera -> stream-republish (F17) -> stream-socket -> mqtt-channel -> beszel
-> connectivity -> disk-hygiene -> net-probe (measures only, last).
boot runs first so that on the tick after a reboot the node.boot line precedes every repair it
caused (cause before consequence in events.jsonl); dropler reads the boot state written that tick.
stream-republish runs AFTER stream-camera: camera repair restarts a *down* stream = a fresh
publish, so F17 only ever fires for an UP-but-stale stream; stream-socket after both, so it only
ever sees an UP-but-wedged one."""
from .dependency import DependencyHealer
from .service_liveness import ServiceLivenessHealer
from .radar_sensor import RadarSensorHealer
from .stream_camera import StreamCameraHealer
from .stream_republish import StreamRepublishHealer
from .beszel_agent import BeszelAgentHealer
from .connectivity import ConnectivityHealer
from .disk_hygiene import DiskHygieneHealer
from .net_probe import NetProbeHealer
from .vegamet_loop import VegametLoopHealer
from .stream_socket import StreamSocketHealer
from .mqtt_channel import MqttChannelHealer
from .node_boot import NodeBootHealer
from .dropler_fitted import DroplerFittedHealer


def default_registry():
    return [
        NodeBootHealer(),      # first: a boot must be on record before anything else acts this tick
        DependencyHealer(),
        ServiceLivenessHealer(),
        RadarSensorHealer(),
        VegametLoopHealer(),   # report-only: what the controller says about its input
        DroplerFittedHealer(), # report-only: FULL mode without the RS485 adapter
        StreamCameraHealer(),
        StreamRepublishHealer(),
        StreamSocketHealer(),  # after the camera/republish repairs: those restart a DOWN stream; this catches an UP-but-wedged one
        MqttChannelHealer(),
        BeszelAgentHealer(),
        ConnectivityHealer(),
        DiskHygieneHealer(),
        NetProbeHealer(),      # measures only; must run last so it sees the settled state
    ]
