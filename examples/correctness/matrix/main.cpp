#include <algorithm>
#include <cmath>
#include <iostream>
#include <iomanip>
#include <string>
#include <vector>
extern "C" void co_sort(int *a, size_t n) { if(n) std::sort(a,a+n); }
#ifndef CO_FUZZ
int main() {
int n; if(!(std::cin>>n)||n<0||n>16) return 1;
std::vector<long long> a(n*n),b(n*n); for(auto &x:a) std::cin>>x; for(auto &x:b) std::cin>>x; for(int r=0;r<n;++r) for(int c=0;c<n;++c) {long long v=0; for(int k=0;k<n;++k) v+=a[r*n+k]*b[k*n+c]; if(r||c) std::cout<<" "; std::cout<<v;}
std::cout<<"\n";
}
#endif
