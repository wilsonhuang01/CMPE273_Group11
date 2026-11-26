"""Testable GPU admission decisions. No driver, reset, or reboot operations are implemented."""

from dataclasses import dataclass

from compiler_optimizer.shared.manifest import require


@dataclass(frozen=True)
class GPURecoveryDecision:
    device_quarantined: bool
    host_drained: bool
    required_action: str
    can_schedule: bool


def assess_gpu_recovery(observation):
    """Only fresh diagnostics plus a known-good smoke test permit readmission.

    The future trusted host supervisor must obtain these observations and persist
    device/host state. This pure policy cannot establish hardware health itself.
    """
    require(
        observation
        in {
            "container_exited",
            "gpu_unresponsive",
            "reset_required",
            "reboot_required",
            "diagnostics_failed",
            "post_recovery_checks_passed",
        },
        "unknown GPU recovery observation",
    )
    if observation == "post_recovery_checks_passed":
        return GPURecoveryDecision(False, False, "none", True)
    if observation in ("gpu_unresponsive", "reboot_required", "diagnostics_failed"):
        return GPURecoveryDecision(True, True, "host_operator_recovery", False)
    return GPURecoveryDecision(
        True,
        False,
        "require_health_checks"
        if observation == "container_exited"
        else "device_operator_recovery",
        False,
    )


@dataclass(frozen=True)
class SimulatedGPULease:
    device_id: str
    owner: str
    fence: int
    expires_ms: int


class SimulatedGPURegistry:
    """In-memory adapter contract for tests, with no hardware operations or health claims.

    A real trusted supervisor must replace this adapter and persist its fences.
    Observations invalidate all leases on a drained host. Readmission needs two
    explicit simulated checks, never merely successful container termination.
    """

    hardware_validated = False

    def __init__(self):
        self.devices = {}
        self.drained_hosts = set()
        self._now = 0

    def _time(self, now_ms):
        require(type(now_ms) is int and now_ms >= self._now, "time must be monotonic")
        self._now = now_ms

    def register(self, device_id, host_id):
        require(isinstance(device_id, str) and 0 < len(device_id) <= 128, "invalid device ID")
        require(isinstance(host_id, str) and 0 < len(host_id) <= 128, "invalid host ID")
        require(device_id not in self.devices, "device already registered")
        self.devices[device_id] = {
            "host": host_id,
            "quarantined": True,
            "fence": 0,
            "lease": None,
        }

    def acquire(self, device_id, owner, *, now_ms, ttl_ms):
        self._time(now_ms)
        require(isinstance(owner, str) and 0 < len(owner) <= 128, "invalid lease owner")
        require(type(ttl_ms) is int and 0 < ttl_ms <= 60000, "lease TTL exceeds bound")
        row = self.devices[device_id]
        require(
            not row["quarantined"] and row["host"] not in self.drained_hosts,
            "device/host is quarantined",
        )
        prior = row["lease"]
        if prior is not None and prior.expires_ms <= now_ms:
            self.observe(device_id, "gpu_unresponsive")
            raise ValueError("expired device lease requires recovery checks")
        require(prior is None, "device is already leased")
        row["fence"] += 1
        lease = SimulatedGPULease(device_id, owner, row["fence"], now_ms + ttl_ms)
        row["lease"] = lease
        return lease

    def release(self, lease, *, now_ms):
        self._time(now_ms)
        row = self.devices[lease.device_id]
        require(row["lease"] == lease and now_ms < lease.expires_ms, "stale GPU lease")
        # An exited container alone does not establish device health.
        self.observe(lease.device_id, "container_exited")

    def observe(self, device_id, observation):
        require(observation != "post_recovery_checks_passed", "use explicit simulated checks")
        decision = assess_gpu_recovery(observation)
        row = self.devices[device_id]
        row["quarantined"] = True
        if decision.host_drained:
            self.drained_hosts.add(row["host"])
        for candidate in self.devices.values():
            if candidate is row or (decision.host_drained and candidate["host"] == row["host"]):
                candidate.update(quarantined=True, lease=None, fence=candidate["fence"] + 1)
        return self._report(device_id, decision.required_action)

    def check(self, lease, *, now_ms):
        self._time(now_ms)
        row = self.devices[lease.device_id]
        if row["lease"] == lease and now_ms >= lease.expires_ms:
            self.observe(lease.device_id, "gpu_unresponsive")
        return (
            row["lease"] == lease
            and not row["quarantined"]
            and row["host"] not in self.drained_hosts
            and now_ms < lease.expires_ms
        )

    def readmit(self, device_id, *, diagnostics_passed, smoke_test_passed):
        require(
            type(diagnostics_passed) is bool and type(smoke_test_passed) is bool,
            "checks must be explicit booleans",
        )
        row = self.devices[device_id]
        require(row["lease"] is None, "cannot readmit a leased device")
        if not (diagnostics_passed and smoke_test_passed):
            return self.observe(device_id, "diagnostics_failed")
        row["quarantined"] = False
        # A host stays drained until every registered device has fresh passing checks.
        if all(not d["quarantined"] for d in self.devices.values() if d["host"] == row["host"]):
            self.drained_hosts.discard(row["host"])
        return self._report(device_id, "none")

    def _report(self, device_id, action):
        row = self.devices[device_id]
        return {
            "device_id": device_id,
            "simulation": True,
            "hardware_validated": False,
            "actual_device_recovery": False,
            "required_action": action,
            "device_quarantined": row["quarantined"],
            "host_drained": row["host"] in self.drained_hosts,
            "can_schedule_simulated": not row["quarantined"]
            and row["host"] not in self.drained_hosts,
        }
