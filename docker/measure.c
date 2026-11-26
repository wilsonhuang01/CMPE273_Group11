#define _GNU_SOURCE
#include "perf.h"
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sched.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

/* Thread-enabled children cannot fork processes or change syscall ABI. The outer
 * daemon filter restricts namespaces/clone flags; clone3 falls back via ENOSYS. */
static int threads_only(void) {
    struct sock_filter code[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
#if defined(__x86_64__)
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
#elif defined(__aarch64__)
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_AARCH64, 1, 0),
#else
#error Unsupported native measurement architecture
#endif
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
#if defined(__x86_64__)
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x40000000, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
#endif
#ifdef SYS_fork
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_fork, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
#endif
#ifdef SYS_vfork
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_vfork, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
#endif
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone, 0, 3),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, CLONE_THREAD, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog filter = {sizeof(code) / sizeof(code[0]), code};
    return prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) ||
           prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &filter);
}

/* Trusted root launcher: only its child executes submitted code, after dropping
 * identity. wait4 measures a fresh native child, excluding Python's pre-exec RSS.
 * /work/run is root-owned, mode 0755; the program cannot forge usage.json. */
int main(int argc, char **argv) {
    if (geteuid() != 0 || argc < 2)
        return 125;
    int threaded = getenv("CO_THREADED") && strcmp(getenv("CO_THREADED"), "1") == 0;
    int perf = getenv("CO_PERF") && strcmp(getenv("CO_PERF"), "1") == 0;
    int transfer[2];
    if (socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0, transfer))
        return 125;
    int ready[2], gate[2];
    if (pipe2(ready, O_CLOEXEC) || pipe2(gate, O_CLOEXEC))
        return 125;
    int errors[2];
    if (pipe2(errors, O_CLOEXEC) != 0)
        return 125;
    pid_t child = fork();
    if (child < 0)
        return 125;
    if (child == 0) {
        close(errors[0]);
        close(transfer[0]);
        close(ready[0]);
        close(gate[1]);
        struct rlimit processes = {threaded ? 4096 : 1, threaded ? 4096 : 1};
        if (setgroups(0, NULL) != 0 || setgid(10001) != 0 || setuid(10001) != 0 ||
            setrlimit(RLIMIT_NPROC, &processes) != 0 || (threaded && threads_only())) {
            int value = errno;
            (void)write(errors[1], &value, sizeof(value));
            _exit(125);
        }
        if (perf) {
            open_counters(0);
            if (!send_counters(transfer[1]))
                _exit(125);
        }
        close(transfer[1]);
        char signal = 1;
        (void)write(ready[1], &signal, 1);
        close(ready[1]);
        if (read(gate[0], &signal, 1) != 1)
            _exit(125);
        close(gate[0]);
        execv(argv[1], argv + 1);
        int value = errno;
        (void)write(errors[1], &value, sizeof(value));
        _exit(125);
    }
    close(transfer[1]);
    close(errors[1]);
    close(ready[1]);
    close(gate[0]);
    char signal;
    if (read(ready[0], &signal, 1) == 1) {
        if (perf && !receive_counters(transfer[0]))
            return 125;
        (void)write(gate[1], &signal, 1);
    }
    close(transfer[0]);
    close(ready[0]);
    close(gate[1]);
    int status = 0, failure = 0;
    struct rusage usage;
    while (wait4(child, &status, 0, &usage) < 0) {
        if (errno != EINTR)
            return 125;
    }
    (void)read(errors[0], &failure, sizeof(failure));
    close(errors[0]);
    int descriptor = open("/work/run/usage.json", O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (descriptor < 0)
        return 125;
    int code = WIFSIGNALED(status) ? -WTERMSIG(status) : WEXITSTATUS(status);
    dprintf(descriptor, "{\"returncode\":%d,\"peak_memory_bytes\":%ld,\"error\":%d", code,
            usage.ru_maxrss * 1024, failure);
    double cpu = usage.ru_utime.tv_sec + usage.ru_utime.tv_usec / 1e6 + usage.ru_stime.tv_sec +
                 usage.ru_stime.tv_usec / 1e6;
    dprintf(descriptor, ",\"cpu_seconds\":%.9f", cpu);
    if (perf)
        write_counters(descriptor);
    dprintf(descriptor, "}\n");
    close(descriptor);
    return 0;
}
