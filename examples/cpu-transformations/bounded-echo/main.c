#include <stdio.h>

int main(void) {
    int reading;
    if (scanf("%d", &reading) != 1 || reading < -4096 || reading > 4096) {
        return 1;
    }
    printf("%d\n", reading);
    return 0;
}
