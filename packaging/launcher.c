/* Qualm.app's executable: the bundled Python, embedded.

   The process is the bundle's own binary, not a child python, so macOS
   attributes Accessibility, Automation and Screen Recording to "Qualm", and
   NSBundle.mainBundle is Qualm.app (menu bar managers list "Qualm").

   No arguments (Finder, launchd): `python -m qualm app`.
   Any arguments: exactly `python ARGS...`, so sys.executable works for
   subprocesses and a CLI shim can run `Qualm -m qualm "$@"`. */

#include <Python.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void fail(PyConfig *config, PyStatus status) {
    PyConfig_Clear(config);
    if (PyStatus_IsExit(status)) exit(status.exitcode);
    Py_ExitStatusException(status);
}

int main(int argc, char **argv) {
    char raw[PATH_MAX], exe[PATH_MAX], home[PATH_MAX];
    uint32_t size = sizeof raw;
    if (_NSGetExecutablePath(raw, &size) != 0 || realpath(raw, exe) == NULL) {
        fprintf(stderr, "Qualm: can't find its own executable\n");
        return 1;
    }
    char dir[PATH_MAX];
    strlcpy(dir, exe, sizeof dir);
    /* <App>/Contents/MacOS/Qualm -> <App>/Contents/Resources/python */
    snprintf(home, sizeof home, "%s/../Resources/python", dirname(dir));
    char resolved[PATH_MAX];
    if (realpath(home, resolved) == NULL) {
        fprintf(stderr, "Qualm: bundled Python missing at %s\n", home);
        return 1;
    }

    PyStatus status;
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.user_site_directory = 0;
    config.write_bytecode = 0;  /* the build compiled everything; a new .pyc would break the signature */
    config.parse_argv = 1;

    status = PyConfig_SetBytesString(&config, &config.home, resolved);
    if (PyStatus_Exception(status)) fail(&config, status);
    status = PyConfig_SetBytesString(&config, &config.executable, exe);
    if (PyStatus_Exception(status)) fail(&config, status);
    status = PyConfig_SetBytesString(&config, &config.program_name, exe);
    if (PyStatus_Exception(status)) fail(&config, status);

    if (argc <= 1) {
        char *app_argv[] = {exe, "-m", "qualm", "app", NULL};
        status = PyConfig_SetBytesArgv(&config, 4, app_argv);
    } else {
        status = PyConfig_SetBytesArgv(&config, argc, argv);
    }
    if (PyStatus_Exception(status)) fail(&config, status);

    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) fail(&config, status);
    PyConfig_Clear(&config);
    return Py_RunMain();
}
