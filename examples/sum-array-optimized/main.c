#include <stdio.h>

/* Same input domain and output as sum-array, with two accumulators. */
int main(void) {
    int count;
    if (scanf("%d", &count) != 1 || count < 0 || count > 100000) {
        return 1;
    }
    long long even = 0, odd = 0;
    for (int i = 0; i < count; ++i) {
        int value;
        if (scanf("%d", &value) != 1 || value < -1000000 || value > 1000000) {
            return 1;
        }
        if (i % 2 == 0) {
            even += value;
        } else {
            odd += value;
        }
    }
    printf("%lld\n", even + odd);
    return 0;
}
