#ifndef COMPILER_OPTIMIZER_PERF_H
#define COMPILER_OPTIMIZER_PERF_H
#include <errno.h>
#include <stdio.h>
#include <sys/types.h>
#include <unistd.h>
/* Trusted per-child, user-space perf counters. No system-wide monitoring. */
#include <linux/perf_event.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/syscall.h>

struct counter {
    const char *name;
    unsigned type;
    unsigned long config;
    int fd, error;
};
static struct counter counters[] = {
    {"cycles", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES, -1, 0},
    {"instructions", PERF_TYPE_HARDWARE, PERF_COUNT_HW_INSTRUCTIONS, -1, 0},
    {"task-clock", PERF_TYPE_SOFTWARE, PERF_COUNT_SW_TASK_CLOCK, -1, 0},
    {"context-switches", PERF_TYPE_SOFTWARE, PERF_COUNT_SW_CONTEXT_SWITCHES, -1, 0},
    {"page-faults", PERF_TYPE_SOFTWARE, PERF_COUNT_SW_PAGE_FAULTS, -1, 0},
};
static void open_counters(pid_t child) {
    for (size_t i = 0; i < sizeof(counters) / sizeof(counters[0]); ++i) {
        struct perf_event_attr attr;
        memset(&attr, 0, sizeof(attr));
        attr.size = sizeof(attr);
        attr.type = counters[i].type;
        attr.config = counters[i].config;
        attr.disabled = 1;
        attr.enable_on_exec = 1;
        attr.inherit = 1;
        attr.exclude_kernel = 1;
        attr.exclude_hv = 1;
        attr.read_format = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
        counters[i].fd = syscall(SYS_perf_event_open, &attr, child, -1, -1, PERF_FLAG_FD_CLOEXEC);
        if (counters[i].fd < 0)
            counters[i].error = errno;
    }
}
/* The unprivileged child opens only its own events, then transfers read handles
 * to the root-owned launcher before exec. Credentials never change for probing. */
static int send_counters(int socket) {
    int fds[5], errors[5], count = 0;
    for (int i = 0; i < 5; ++i) {
        errors[i] = counters[i].error;
        if (counters[i].fd >= 0)
            fds[count++] = counters[i].fd;
    }
    char control[CMSG_SPACE(sizeof(fds))] = {0};
    struct iovec data = {errors, sizeof(errors)};
    struct msghdr message = {0};
    message.msg_iov = &data;
    message.msg_iovlen = 1;
    if (count) {
        message.msg_control = control;
        message.msg_controllen = CMSG_SPACE(count * sizeof(int));
        struct cmsghdr *header = CMSG_FIRSTHDR(&message);
        header->cmsg_level = SOL_SOCKET;
        header->cmsg_type = SCM_RIGHTS;
        header->cmsg_len = CMSG_LEN(count * sizeof(int));
        memcpy(CMSG_DATA(header), fds, count * sizeof(int));
    }
    int ok = sendmsg(socket, &message, 0) == sizeof(errors);
    for (int i = 0; i < count; ++i)
        close(fds[i]);
    return ok;
}
static int receive_counters(int socket) {
    int errors[5], count = 0;
    char control[CMSG_SPACE(5 * sizeof(int))] = {0};
    struct iovec data = {errors, sizeof(errors)};
    struct msghdr message = {0};
    message.msg_iov = &data;
    message.msg_iovlen = 1;
    message.msg_control = control;
    message.msg_controllen = sizeof(control);
    if (recvmsg(socket, &message, MSG_CMSG_CLOEXEC) != sizeof(errors) ||
        (message.msg_flags & (MSG_CTRUNC | MSG_TRUNC)))
        return 0;
    struct cmsghdr *header = CMSG_FIRSTHDR(&message);
    int *fds = header ? (int *)CMSG_DATA(header) : NULL;
    int available = header ? (header->cmsg_len - CMSG_LEN(0)) / sizeof(int) : 0;
    if (header && (header->cmsg_level != SOL_SOCKET || header->cmsg_type != SCM_RIGHTS))
        return 0;
    for (int i = 0; i < 5; ++i) {
        counters[i].error = errors[i];
        if (!errors[i]) {
            if (count >= available)
                return 0;
            counters[i].fd = fds[count++];
        }
    }
    return count == available;
}
static void write_counters(int output) {
    dprintf(output, ",\"perf\":{");
    for (size_t i = 0; i < sizeof(counters) / sizeof(counters[0]); ++i) {
        uint64_t data[3] = {0};
        struct counter *c = &counters[i];
        if (c->fd >= 0) {
            if (read(c->fd, data, sizeof(data)) != sizeof(data) || !data[1] || !data[2])
                c->error = ENODATA;
            close(c->fd);
        }
        dprintf(output, "%s\"%s\":{\"status\":\"%s\",\"errno\":%d,\"count\":", i ? "," : "",
                c->name, c->error ? "unavailable" : "available", c->error);
        if (c->error)
            dprintf(output, "null");
        else
            dprintf(output, "%llu", (unsigned long long)data[0]);
        dprintf(output, ",\"time_enabled_ns\":%llu,\"time_running_ns\":%llu}",
                (unsigned long long)data[1], (unsigned long long)data[2]);
    }
    dprintf(output, ",\"energy-pkg\":{\"status\":\"unavailable\",\"count\":null,"
                    "\"reason\":\"package-wide energy is unavailable in the per-task sandbox\"}}");
}

#endif
