#include <stdio.h>

/* Input: count in [0, 100000], followed by count integers in [-1000000, 1000000]. */
int main(void) {
    int count;
    if (scanf("%d", &count) != 1 || count < 0 || count > 100000) {
        return 1;
    }

    long long sum = 0;
    for (int i = 0; i < count; ++i) {
        int value;
        if (scanf("%d", &value) != 1 || value < -1000000 || value > 1000000) {
            return 1;
        }
        sum += value;
    }

    printf("%lld\n", sum);
    return 0;
}
