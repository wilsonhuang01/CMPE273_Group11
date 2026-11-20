"""Controlled architecture admission, emulator child filtering and GPU recovery contracts."""

import pytest
from test_contracts import compiler

from compiler_optimizer.shared.gpu_recovery import assess_gpu_recovery
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.workers.capabilities import describe_local
from compiler_optimizer.workers.emulator_policy import instructions
from compiler_optimizer.workers.targets import compiler_flags, target_profile


def test_riscv_compiler_profile_is_explicit_and_cannot_take_x86_flags():
    from compiler_optimizer.shared.contracts import _compiler

    value = compiler()
    value.target = "linux-riscv64"
    with pytest.raises(ContractError):
        _compiler(value)
    value.flags[:] = compiler_flags(value.target, value.language)
    _compiler(value)
    assert not target_profile(value.target).performance_qualification
    value.target = "cuda"
    with pytest.raises(ContractError):
        _compiler(value)


def test_cuda_is_unavailable_without_a_real_backend():
    capabilities = describe_local()
    assert capabilities["cuda"]["available"] is False
    assert capabilities["cuda"]["execution_enabled"] is False
    assert all(t["performance_qualification"] is False for t in capabilities["targets"])
    with pytest.raises(ValueError):
        target_profile("cuda")


def test_incompatible_target_is_skipped_without_quarantining_a_healthy_worker(tmp_path):
    from test_recovery import setup

    repo, first, second, task, _, recovery, coordinator, _ = setup(tmp_path)
    coordinator.runner = second
    original_compiler = first.compiler

    def riscv_compiler(language):
        value = original_compiler(language)
        value.target = "linux-riscv64"
        value.flags[:] = compiler_flags(value.target, language)
        return value

    first.compiler = riscv_compiler
    worker = recovery.workers[0]
    repo.register_recovery_worker(task, worker.worker_id, worker.incarnation_id)
    assert coordinator.run(task, agents=["performance"])["status"] == "selected"
    record = next(w for w in repo.recovery_workers() if w["worker_id"] == worker.worker_id)
    assert record["state"] == "idle" and record["quarantine_until_ms"] == 0
    proposal = repo.list_proposals(task)[0]
    assert repo.recovery_attempts(task, proposal.proposal_id)[0]["worker_id"] != worker.worker_id


@pytest.mark.parametrize(
    "observation",
    [
        "container_exited",
        "gpu_unresponsive",
        "reset_required",
        "reboot_required",
        "diagnostics_failed",
    ],
)
def test_gpu_faults_remain_quarantined_until_post_recovery_checks(observation):
    result = assess_gpu_recovery(observation)
    assert result.device_quarantined and not result.can_schedule
    if observation in ("gpu_unresponsive", "reboot_required", "diagnostics_failed"):
        assert result.host_drained
    assert assess_gpu_recovery("post_recovery_checks_passed").can_schedule
    with pytest.raises(ContractError):
        assess_gpu_recovery("container_was_killed_so_gpu_is_healthy")


def interpret(program, *, arch, number, flags=0):
    """Exercise the exact classic BPF emitted for Linux, without restricting pytest's host."""
    pc, accumulator = 0, 0
    while pc < len(program):
        code, yes, no, value = program[pc]
        if code == 0x20:
            accumulator = {0: number, 4: arch, 16: flags}[value]
        elif code == 0x15:
            pc += yes if accumulator == value else no
        elif code == 0x45:
            pc += yes if accumulator & value else no
        elif code == 0x06:
            return value
        else:
            raise AssertionError("unexpected BPF opcode")
        pc += 1
    raise AssertionError("unterminated BPF")


@pytest.mark.parametrize("machine,arch", [("aarch64", 0xC00000B7), ("x86_64", 0xC000003E)])
def test_qemu_filter_allows_threads_and_blocks_processes_and_abi_bypasses(machine, arch):
    numbers = {"fork": 57, "vfork": 58, "clone": 56, "clone3": 435}
    program = instructions(machine, numbers.__getitem__)

    def run(n, **kw):
        return interpret(program, arch=arch, number=n, **kw)

    assert run(1) == 0x7FFF0000
    assert run(57) == run(58) == run(56, flags=17) == 0x00050001
    assert run(435) == 0x00050026
    assert run(56, flags=0x10000 | 0x100) == 0x7FFF0000
    assert interpret(program, arch=0x40000003, number=1) == 0x80000000
    if machine == "x86_64":
        assert run(0x40000000 + 56, flags=17) == 0x80000000
