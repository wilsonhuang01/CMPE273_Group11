#include <stdio.h>

void transform(const unsigned int *input, unsigned int *output, unsigned int length) {
    for (unsigned int index = 0; index < length; ++index) {
        output[index] = input[index] ^ 90U;
    }
}

int main(void) {
    unsigned int length, mode;
    unsigned int data[66], separate[66];
    if (scanf("%u%u", &length, &mode) != 2 || length > 64 || mode > 3) {
        return 1;
    }
    for (unsigned int i = 0; i <= length; ++i) {
        if (scanf("%u", &data[i]) != 1) {
            return 1;
        }
    }
    const unsigned int *input = data + (mode == 3);
    unsigned int *output = mode == 0 ? separate : data + (mode == 2);
    transform(input, output, length);
    for (unsigned int i = 0; i < length; ++i) {
        printf("%u%c", output[i], i + 1 == length ? '\n' : ' ');
    }
    if (length == 0) {
        puts("");
    }
    return 0;
}
