"""The ordered registry. Run order is least-invasive / dependency-first:
dependency (redis) -> liveness -> radar -> stream-camera -> stream-republish (F17)
-> beszel -> connectivity -> disk-hygiene. (stream-republish runs AFTER
stream-camera: camera repair restarts a *down* stream = a fresh publish, so F17
only ever fires for an UP-but-stale stream.)"""
from .dependency import DependencyHealer
from .service_liveness import ServiceLivenessHealer
from .radar_sensor import RadarSensorHealer
from .stream_camera import StreamCameraHealer
from .stream_republish import StreamRepublishHealer
from .beszel_agent import BeszelAgentHealer
from .connectivity import ConnectivityHealer
from .disk_hygiene import DiskHygieneHealer


def default_registry():
    return [
        DependencyHealer(),
        ServiceLivenessHealer(),
        RadarSensorHealer(),
        StreamCameraHealer(),
        StreamRepublishHealer(),
        BeszelAgentHealer(),
        ConnectivityHealer(),
        DiskHygieneHealer(),
    ]
