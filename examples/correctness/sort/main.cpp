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
std::vector<int> a(n); for(auto &x:a) std::cin>>x; co_sort(a.data(),a.size()); for(size_t i=0;i<a.size();++i) {if(i) std::cout<<" "; std::cout<<a[i];}
std::cout<<"\n";
}
#endif
