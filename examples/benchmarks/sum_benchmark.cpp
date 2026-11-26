
#include <benchmark/benchmark.h>
#include <cstdint>
#include <cstdlib>
#include <vector>
extern "C" int64_t co_sum(const int64_t *, size_t);
static void Kernel(benchmark::State &state) {
    std::vector<int64_t> a(4096), b(4096);
    for (size_t i = 0; i < a.size(); ++i) {
        a[i] = static_cast<int64_t>((i * 17 + 273ULL) % 33) - 16;
        b[i] = static_cast<int64_t>((i * 7 + 273ULL) % 17) - 8;
    }
    for (int i = 0; i < 2; ++i)
        if (co_sum(a.data(), a.size()) != 8)
            std::abort();
    for (auto _ : state) {
        auto result = co_sum(a.data(), a.size());
        benchmark::DoNotOptimize(result);
        if (result != 8)
            std::abort();
        benchmark::ClobberMemory();
    }
    state.SetItemsProcessed(state.iterations() * 4096);
}
BENCHMARK(Kernel)->Iterations(64)->Repetitions(5)->MeasureProcessCPUTime()->UseRealTime();
BENCHMARK_MAIN();
