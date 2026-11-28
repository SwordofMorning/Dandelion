# ===== Step 1 : Project and C++17 Configuration =====

cmake_minimum_required(VERSION 3.26)
project(dandelion_core_native LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)

if(MSVC)
    # Link the static C++ runtime (/MT) across both nanobind-static and _native so
    # prebuilt Windows wheels never require host-installed MSVCP140.dll.
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded")
endif()

# ===== Step 2 : Python and nanobind Discovery =====

find_package(Python REQUIRED COMPONENTS Interpreter Development.Module OPTIONAL_COMPONENTS Development.SABIModule)

if(Python_VERSION VERSION_GREATER_EQUAL "3.12" AND NOT TARGET Python::SABIModule)
    message(FATAL_ERROR "Python ${Python_VERSION} requires Development.SABIModule for cp312-abi3 builds.")
endif()

execute_process(
    COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
    OUTPUT_STRIP_TRAILING_WHITESPACE
    OUTPUT_VARIABLE DANDELION_NANOBIND_CMAKE_DIR
    RESULT_VARIABLE DANDELION_NANOBIND_QUERY_RESULT
)
if(DANDELION_NANOBIND_QUERY_RESULT EQUAL 0 AND EXISTS "${DANDELION_NANOBIND_CMAKE_DIR}")
    list(APPEND CMAKE_PREFIX_PATH "${DANDELION_NANOBIND_CMAKE_DIR}")
endif()

find_package(nanobind CONFIG REQUIRED)

# ===== Step 3 : Native Source Inventory =====

set(DANDELION_NATIVE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/src/dandelion_core/utils/llm_provider/native")

set(DANDELION_NATIVE_SOURCES
    "${DANDELION_NATIVE_DIR}/module.cpp"
    "${DANDELION_NATIVE_DIR}/common.cpp"
    "${DANDELION_NATIVE_DIR}/provider.cpp"
    "${DANDELION_NATIVE_DIR}/api_messages.cpp"
    "${DANDELION_NATIVE_DIR}/api_chat_completions.cpp"
    "${DANDELION_NATIVE_DIR}/api_responses.cpp"
    "${DANDELION_NATIVE_DIR}/api_generate_content.cpp"
    "${DANDELION_NATIVE_DIR}/api_interactions.cpp"
)

if(WIN32)
    list(APPEND DANDELION_NATIVE_SOURCES "${DANDELION_NATIVE_DIR}/http_winhttp.cpp")
else()
    list(APPEND DANDELION_NATIVE_SOURCES "${DANDELION_NATIVE_DIR}/http_curl.cpp")
endif()

# ===== Step 4 : Build nanobind Extension (cp312-abi3 on Python >= 3.12) =====

if(Python_VERSION VERSION_GREATER_EQUAL "3.12")
    message(STATUS "Building dandelion _native with nanobind STABLE_ABI (cp312-abi3) and NB_STATIC")
    nanobind_add_module(_native STABLE_ABI NB_STATIC ${DANDELION_NATIVE_SOURCES})
else()
    message(STATUS "Building dandelion _native with nanobind NB_STATIC (Python ${Python_VERSION})")
    nanobind_add_module(_native NB_STATIC ${DANDELION_NATIVE_SOURCES})
endif()

target_include_directories(_native PRIVATE "${DANDELION_NATIVE_DIR}")

# ===== Step 5 : Platform Transport and Compiler Flags =====

if(WIN32)
    if(MSVC)
        set_property(TARGET _native PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded")
        target_compile_options(_native PRIVATE /EHsc /O2 /utf-8 /bigobj)
    else()
        target_compile_options(_native PRIVATE -O2)
        target_link_options(_native PRIVATE -static -static-libgcc -static-libstdc++)
    endif()
    target_link_libraries(_native PRIVATE winhttp)
else()
    if(NOT APPLE)
        set(DANDELION_EXPORT_MAP "${CMAKE_CURRENT_BINARY_DIR}/native_exports.map")
        file(WRITE "${DANDELION_EXPORT_MAP}" "{\n    global:\n        PyInit__native;\n    local:\n        *;\n};\n")
        target_link_options(_native PRIVATE "-Wl,--version-script=${DANDELION_EXPORT_MAP}")
    endif()

    set(DANDELION_STATIC_PREFIX "$ENV{DANDELION_STATIC_PREFIX}")
    if(NOT APPLE AND DANDELION_STATIC_PREFIX AND EXISTS "${DANDELION_STATIC_PREFIX}/lib/libcurl.a")
        message(STATUS "Linking static libcurl + OpenSSL + zlib from ${DANDELION_STATIC_PREFIX}")
        target_compile_definitions(_native PRIVATE CURL_STATICLIB)
        target_include_directories(_native PRIVATE "${DANDELION_STATIC_PREFIX}/include")
        target_link_libraries(_native PRIVATE
            "${DANDELION_STATIC_PREFIX}/lib/libcurl.a"
            "${DANDELION_STATIC_PREFIX}/lib/libssl.a"
            "${DANDELION_STATIC_PREFIX}/lib/libcrypto.a"
            "${DANDELION_STATIC_PREFIX}/lib/libz.a"
            dl
            pthread
        )
        target_compile_options(_native PRIVATE -O2 -ffunction-sections -fdata-sections)
        target_link_options(_native PRIVATE -Wl,--exclude-libs,ALL -Wl,--gc-sections)
    else()
        # Local Linux / Conda / macOS dev build: also hint Python base_prefix and CONDA_PREFIX
        # so Conda environments without system libcurl4-openssl-dev compile out of the box.
        execute_process(
            COMMAND "${Python_EXECUTABLE}" -c "import sys; print(sys.base_prefix)"
            OUTPUT_STRIP_TRAILING_WHITESPACE
            OUTPUT_VARIABLE DANDELION_PYTHON_BASE_PREFIX
            RESULT_VARIABLE DANDELION_BASE_PREFIX_RESULT
        )
        if(DANDELION_BASE_PREFIX_RESULT EQUAL 0 AND EXISTS "${DANDELION_PYTHON_BASE_PREFIX}")
            list(APPEND CMAKE_PREFIX_PATH "${DANDELION_PYTHON_BASE_PREFIX}")
        endif()
        if(DEFINED Python_ROOT_DIR AND EXISTS "${Python_ROOT_DIR}")
            list(APPEND CMAKE_PREFIX_PATH "${Python_ROOT_DIR}")
        endif()
        if(DEFINED ENV{CONDA_PREFIX} AND EXISTS "$ENV{CONDA_PREFIX}")
            list(APPEND CMAKE_PREFIX_PATH "$ENV{CONDA_PREFIX}")
        endif()

        find_package(CURL REQUIRED)
        message(STATUS "Linking system/environment libcurl: ${CURL_LIBRARIES} (headers: ${CURL_INCLUDE_DIRS})")
        target_compile_options(_native PRIVATE -O2)
        target_link_libraries(_native PRIVATE CURL::libcurl)
    endif()
endif()

# ===== Step 6 : Install Target into dandelion_core Package =====

install(TARGETS _native DESTINATION dandelion_core/utils/llm_provider)
