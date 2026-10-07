#include "service.h"
#include "admin.h"
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--version")) {
        puts("stelvio-tunnel-helper/1"); return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "--serve")) return stlv_service();
    if (argc == 2 && !strcmp(argv[1], "--install")) return stlv_admin(1);
    if (argc == 2 && !strcmp(argv[1], "--uninstall")) return stlv_admin(0);
    return 2;
}
