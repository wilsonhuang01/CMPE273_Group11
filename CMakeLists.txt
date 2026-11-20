cmake_minimum_required(VERSION 3.25)
project(DistributedCompilerOptimizer VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_C_STANDARD 17)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Only repository-owned examples are built here. Submitted code will be compiled
# separately by the sandbox workers using the controlled submission contract.
add_executable(sum_array examples/sum-array/main.c)
set_target_properties(sum_array PROPERTIES
    OUTPUT_NAME sum-array
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
)
if(CMAKE_C_COMPILER_ID MATCHES "Clang|GNU")
    target_compile_options(sum_array PRIVATE -Wall -Wextra -Werror)
endif()

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 3.11 REQUIRED COMPONENTS Interpreter)
    add_test(
        NAME example.sum-array
        COMMAND "${Python3_EXECUTABLE}" -m pytest
            "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_example.py"
            --program "$<TARGET_FILE:sum_array>"
    )
    set_tests_properties(example.sum-array PROPERTIES TIMEOUT 30)
endif()
