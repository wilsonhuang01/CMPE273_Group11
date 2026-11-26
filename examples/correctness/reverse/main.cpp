#include <algorithm>
#include <cmath>
#include <iostream>
#include <iomanip>
#include <string>
#include <vector>
extern "C" void co_sort(int *a, size_t n) { if(n) std::sort(a,a+n); }
#ifndef CO_FUZZ
int main() {
std::string s; std::getline(std::cin,s); std::reverse(s.begin(),s.end()); std::cout<<s;
std::cout<<"\n";
}
#endif
