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
