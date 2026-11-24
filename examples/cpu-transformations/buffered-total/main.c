#include <stdio.h>
#include <stdlib.h>

/* Input: length in [0, 4096], then length integers in [-4096, 4096]. */
int main(void) {
    int length;
    if (scanf("%d", &length) != 1 || length < 0 || length > 4096) {
        return 1;
    }
    long long *samples = malloc(((size_t)length + 1) * sizeof(*samples));
    if (samples == NULL) {
        return 1;
    }
    for (int i = 0; i < length; ++i) {
        if (scanf("%lld", &samples[i]) != 1 || samples[i] < -4096 || samples[i] > 4096) {
            free(samples);
            return 1;
        }
    }
    long long total = 0;
    for (int i = 0; i < length; ++i) {
        total += samples[i];
    }
    free(samples);
    printf("%lld\n", total);
    return 0;
}
