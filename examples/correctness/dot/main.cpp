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
std::vector<double> a(n),b(n); for(auto &x:a) std::cin>>x; for(auto &x:b) std::cin>>x; double v=0; for(int i=0;i<n;++i) v+=a[i]*b[i]; std::cout<<std::setprecision(17)<<v;
std::cout<<"\n";
}
#endif
