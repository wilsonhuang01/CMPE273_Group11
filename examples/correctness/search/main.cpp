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
std::vector<int> a(n); for(auto &x:a) std::cin>>x; int key; std::cin>>key; int found=-1; for(int i=0;i<n;++i) if(a[i]==key) {found=i;break;} std::cout<<found;
std::cout<<"\n";
}
#endif
