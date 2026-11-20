#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

/* Trusted root launcher: only its child executes submitted code, after dropping
 * identity. wait4 measures a fresh native child, excluding Python's pre-exec RSS.
 * /work/run is root-owned, mode 0755; the program cannot forge usage.json. */
int main(int argc, char **argv) {
    if (geteuid() != 0 || argc < 2)
        return 125;
    int errors[2];
    if (pipe2(errors, O_CLOEXEC) != 0)
        return 125;
    pid_t child = fork();
    if (child < 0)
        return 125;
    if (child == 0) {
        close(errors[0]);
        struct rlimit processes = {1, 1};
        if (setgroups(0, NULL) != 0 || setgid(10001) != 0 || setuid(10001) != 0 ||
            setrlimit(RLIMIT_NPROC, &processes) != 0) {
            int value = errno;
            (void)write(errors[1], &value, sizeof(value));
            _exit(125);
        }
        execv(argv[1], argv + 1);
        int value = errno;
        (void)write(errors[1], &value, sizeof(value));
        _exit(125);
    }
    close(errors[1]);
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
    dprintf(descriptor, "{\"returncode\":%d,\"peak_memory_bytes\":%ld,\"error\":%d}\n", code,
            usage.ru_maxrss * 1024, failure);
    close(descriptor);
    return 0;
}
