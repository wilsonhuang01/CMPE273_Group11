#include <cstdint>
#include <iostream>
#include <omp.h>
#include <vector>

extern "C" int64_t co_sum(const int64_t *a, size_t n) {
    int64_t result = 0;
#pragma omp parallel for num_threads(CO_THREADS) reduction(+ : result) schedule(static)
    for (size_t i = 0; i < n; ++i)
        result += a[i];
    return result;
}
#ifndef CO_BENCHMARK
int main() {
    size_t n;
    if (!(std::cin >> n) || n > 65536)
        return 1;
    std::vector<int64_t> a(n);
    for (auto &x : a)
        if (!(std::cin >> x) || x < -1000 || x > 1000)
            return 1;
    std::cout << co_sum(a.data(), a.size()) << '\n';
}
#endif
