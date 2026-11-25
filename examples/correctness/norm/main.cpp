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
double v=0,x; for(int i=0;i<n;++i) {std::cin>>x; v+=x*x;} std::cout<<std::setprecision(17)<<std::sqrt(v);
std::cout<<"\n";
}
#endif
