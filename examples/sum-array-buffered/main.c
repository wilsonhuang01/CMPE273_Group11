#include <stdio.h>
#include <stdlib.h>

/* Input: count in [0, 1000000], then count integers in [-1000000, 1000000]. */
int main(void) {
    int count;
    if (scanf("%d", &count) != 1 || count < 0 || count > 1000000) {
        return 1;
    }
    long long *values = malloc(((size_t)count + 1) * sizeof(*values));
    if (values == NULL) {
        return 1;
    }
    for (int i = 0; i < count; ++i) {
        if (scanf("%lld", &values[i]) != 1 || values[i] < -1000000 || values[i] > 1000000) {
            free(values);
            return 1;
        }
    }
    long long sum = 0;
    for (int i = 0; i < count; ++i) {
        sum += values[i];
    }
    free(values);
    printf("%lld\n", sum);
    return 0;
}
