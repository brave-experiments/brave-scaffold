# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Public command definitions. Help, parsing, and dispatch all read this table."""

from __future__ import annotations

from ..common.cli import CommandSpec, Opt, Positional
from . import android, clean, cmd_build, cmd_patches, cmd_setup, cmd_test_local, cmd_tools, doctor

WITH_PYTHONPATH = Opt("--with-pythonpath", "with_pythonpath", takes_value=False,
                      help="Also export PYTHONPATH for Core's script directory.")
CWD = Opt("--cwd", "cwd", metavar="DIRECTORY",
          help="Run in this directory (relative to your current directory) instead of the current one.")

CONFIGURATION = Opt("--configuration", "configuration", choices=("debug", "release"),
                    help="Build configuration (default: debug).", metavar="CONFIG")
OFFLINE = Opt("--offline", "offline", takes_value=False,
              help="Compile locally instead of using remote build execution (RBE/Siso, the default).")
FORCE_GN = Opt("--force-gn", "force_gn", takes_value=False, help="Regenerate GN files even if they exist.")
PLAN = Opt("--plan", "plan", takes_value=False, help="Show the steps without running any that change anything.")
SKIP_SUPPORT_REFRESH = Opt("--skip-support-refresh", "skip_support_refresh", takes_value=False,
                           help="Stop an Android build if support patches or resources need refreshing.")
ARTIFACT = Opt("--artifact", "artifact", metavar="PATH", help="Application to run instead of the default output.")
FILTER = Opt("--filter", "filter", metavar="PATTERN", help="Only run tests matching the pattern within the suite.")
DEVICE = Opt("--device", "device", metavar="ID", help="Android device id (required when several are usable), or an iOS Simulator name or UDID.")
SOURCE = Opt("--source", "source", metavar="URL_OR_PATH", help="Support repository to clone (default: the standard source).")
REF = Opt("--ref", "ref", metavar="REF", help="Shared support repository branch, tag, or commit.")
DIFF = Opt("--diff", "diff", takes_value=False, help="Print the Git diff of each drifted file.")
BASE = Opt("--base", "base", metavar="REF", help="Compare the branch with this ref (default: origin/master).")
SCOPE = Opt("--scope", "scope", choices=("both", "committed", "worktree"), metavar="SCOPE",
            help="Which changes to inspect: committed branch changes, the working tree, or both (default).")
TARGET = Positional("target", help="mac, android, or ios (default: configured platform, else this host).")
BUILD_SIDE_EFFECTS = ("Writes the build output under the checkout's src/out, applies Core patches when they are "
                      "out of date and no local edits are at risk, and may update the Metal toolchain setting for the "
                      "child only. Android support refresh may reset patch targets and replace or sign resources in "
                      "its declared paths. Never cleans, installs, or launches anything.")

# Commands that take a group word first ("checkout add"). The value is the set of subcommands.
GROUPS = {"checkout": ("add", "list"), "env": ("init", "export", "check"), "tools": ("setup",),
          "patches": ("update",), "android": ("setup",)}


def build_registry():
    specs = [
        CommandSpec("cd", "Print a checkout directory; the shell function changes directory.",
                    cmd_setup.cd, positionals=(Positional("checkout", required=True, help="Checkout alias or path."),),
                    examples=("bdev cd main", "bdev cd alt-1"),
                    notes="Source scripts/bdev-shell.sh in Bash or Zsh to change the current shell directory."),
        CommandSpec("context", "Show the resolved checkout, environment mapping, configuration, and tool state.",
                    cmd_setup.context, examples=("bdev context", "bdev context --checkout main --json")),
        CommandSpec("capabilities", "List supported, limited, unverified, and unsupported combinations.",
                    cmd_setup.capabilities, examples=("bdev capabilities --json",)),
        CommandSpec("doctor", "Check readiness for a scope without repairing anything.", doctor.run_doctor,
                    positionals=(Positional("scope", help="mac, android, ios, rbe, shell, or signing; omit for all scopes."),),
                    examples=("bdev doctor", "bdev doctor mac --checkout main"),
                    notes="Checking a selected checkout evaluates its approved environment file. Refresh warnings do not guarantee a build can repair the files."),
        CommandSpec("setup", "Inspect prerequisites and prepare scaffold-owned configuration.", cmd_setup.setup, creates_config=True,
                    side_effects="Creates brave-scaffold.toml beside the scaffold when it is missing. "
                                 "Nothing inside Brave Core changes.",
                    examples=("bdev setup",)),
        CommandSpec("checkout add", "Register an existing checkout under a name.", cmd_setup.checkout_add, creates_config=True,
                    positionals=(Positional("name", True, help="Alias for the checkout."),
                                 Positional("path", True, help="Core, Chromium src, or outer checkout path.")),
                    side_effects="Edits brave-scaffold.toml. Nothing inside the checkout changes.",
                    examples=("bdev checkout add main /work/browser/_bad_scm/workspace/src/brave",)),
        CommandSpec("checkout list", "List registered checkouts, environment state, and invalid registrations.",
                    cmd_setup.checkout_list, examples=("bdev checkout list",)),
        CommandSpec("env init", "Generate the scaffold-owned environment for a checkout and print the approval command.",
                    cmd_setup.env_init,
                    side_effects="Writes <environment-dir>/.envrc and the checkout record in brave-scaffold.toml. "
                                 "Never approves the file and never writes inside Brave Core.",
                    examples=("bdev env init --checkout main",)),
        CommandSpec("env export", "Print derived checkout exports for a generated .envrc (no direnv, no tool changes).",
                    cmd_setup.env_export, options=(WITH_PYTHONPATH,),
                    examples=("bdev env export --checkout main --format bash",)),
        CommandSpec("env check", "Compare the loaded environment against the checkout identity and tools.",
                    cmd_setup.env_check, options=(WITH_PYTHONPATH,),
                    notes="Evaluates the approved environment file, which is user-reviewed code.",
                    examples=("bdev env check --checkout main",)),
        CommandSpec("shell", "Start a child shell in Core using the approved environment.", cmd_setup.shell,
                    notes="Exiting the shell leaves your original environment unchanged.",
                    examples=("bdev shell --checkout main",)),
        CommandSpec("tools setup", "Explicitly provision or repair checkout-local Node and package-manager payloads.",
                    cmd_tools.tools_setup,
                    side_effects="Downloads and deploys the checkout's pinned payloads inside the checkout's "
                                 "third_party/node directory using the checkout's own installer.",
                    examples=("bdev tools setup --checkout main",)),
        CommandSpec("vpython3", "Run the checkout-local vpython3 with your arguments.", cmd_tools.vpython3,
                    options=(CWD,), forward=True, leading_only=True,
                    side_effects="Whatever the Python program does. Runs in your current directory unless --cwd is given.",
                    notes="Scaffold options must come before the first Python argument. Use -- to be explicit.",
                    examples=("bdev vpython3 -- tools/example.py --flag", "bdev vpython3 --cwd out -- ../script.py")),
        clean.SPEC,
        CommandSpec("build", "Prepare and compile Brave for a target, then verify its output.", cmd_build.cmd_build,
                    positionals=(TARGET,), options=(CONFIGURATION, OFFLINE, FORCE_GN, PLAN, DEVICE, SKIP_SUPPORT_REFRESH),
                    forward=True, side_effects=BUILD_SIDE_EFFECTS + " iOS: runs xcodebuild, whose Debug scheme "
                    "builds Core's output under src/out and repoints out/ios_current_link.",
                    notes="Unknown options and extra arguments go to 'bpm run build' after the generated ones; "
                          "for iOS they go to xcodebuild. --device chooses the iOS Simulator to build for.",
                    examples=("bdev build", "bdev build mac --offline", "bdev build --plan")),
        CommandSpec("build-run", "Build, then restart the browser with exactly the output that build produced.",
                    cmd_build.cmd_build_run, aliases=("br",), positionals=(TARGET,),
                    options=(CONFIGURATION, OFFLINE, FORCE_GN, PLAN, DEVICE, SKIP_SUPPORT_REFRESH), forward=True,
                    side_effects=BUILD_SIDE_EFFECTS + " Then stops any running instance of the same application and "
                                                     "launches the new build.",
                    examples=("bdev br",)),
        CommandSpec("sync", "Run Core's source sync command.", cmd_build.cmd_sync,
                    positionals=(Positional("targets", help="Comma-separated targets: mac, android, ios."),),
                    options=(PLAN,), forward=True,
                    side_effects="Runs Core's sync, including its resets, patches, and hooks; local changes may be overwritten. "
                                 "Mobile targets keep the checkout's existing target_os values.",
                    examples=("bdev sync", "bdev sync mac,android --plan")),
        CommandSpec("sync-build", "Sync, then build; stops at the first failed phase.", cmd_build.cmd_sync_build,
                    aliases=("sb",), positionals=(TARGET,),
                    options=(CONFIGURATION, OFFLINE, FORCE_GN, PLAN, DEVICE, SKIP_SUPPORT_REFRESH),
                    forward=True, side_effects="Sync effects, then build effects.", examples=("bdev sb",),
                    notes="Extra arguments go to the build phase only."),
        CommandSpec("sync-build-run", "Sync, build, then restart the browser with the built output.",
                    cmd_build.cmd_sync_build_run, aliases=("sbr",), positionals=(TARGET,),
                    options=(CONFIGURATION, OFFLINE, FORCE_GN, PLAN, DEVICE, SKIP_SUPPORT_REFRESH), forward=True,
                    side_effects="Sync, build, and restart effects.", examples=("bdev sbr",),
                    notes="Extra arguments go to the build phase only."),
        CommandSpec("test", "Compile if needed and run one test suite (macOS, or Android JUnit and device tests).",
                    cmd_build.cmd_test,
                    positionals=(TARGET, Positional("suite", True, help="Test suite, for example brave_unit_tests; on "
                                                    "Android brave_junit_tests or brave_java_unit_tests.")),
                    options=(CONFIGURATION, OFFLINE, FILTER, PLAN, DEVICE), forward=True, max_positionals=2,
                    post_parse=cmd_build.post_parse_test,
                    side_effects=BUILD_SIDE_EFFECTS.replace("Never cleans, installs, or launches anything.",
                                                            "Runs the tests, which may launch test browsers.")
                    + " Android: also requires the support working copy on the android-testing-prototype branch "
                      "(never switched), applies the support repository's test overlay to Core's build/commands "
                      "and leaves it applied, builds in out/android_tests_<configuration>_arm64, and "
                      "brave_java_unit_tests runs on the selected device.",
                    notes="The suite must come before any forwarded arguments. --filter only narrows the suite. "
                          "On Android, brave_junit_tests runs on this Mac and takes no --device; "
                          "brave_java_unit_tests needs a device (--device is a scaffold option, not forwarded). "
                          "Host-side filters need a fully qualified class or a wildcard such as '*ExampleTest*'.",
                    examples=("bdev test brave_unit_tests", "bdev test mac brave_browser_tests --filter 'Example.*'",
                              "bdev test android brave_junit_tests --filter='*BraveCommandLineInitUtilTest*'",
                              "bdev test android brave_java_unit_tests --filter='BraveAppearancePreferencesTest.*' "
                              "--device=emulator-5554")),
        CommandSpec("test-local", "Run the tests this branch or working tree modifies, one suite after another.",
                    cmd_test_local.cmd_test_local, positionals=(TARGET,), options=(BASE, SCOPE, CONFIGURATION, OFFLINE, DEVICE, PLAN),
                    side_effects=BUILD_SIDE_EFFECTS.replace("Never cleans, installs, or launches anything.",
                                                            "Runs the tests, which may launch test browsers.")
                    + " Reads Git state only to choose tests. Android phases need the android-testing-prototype "
                      "support branch and apply the test overlay (see 'bdev test --help').",
                    notes="Finds modified Android javatests and junit tests, C++ unit and browser tests, and desktop "
                          "WebUI tests; builds the filters from the files; runs each suite with 'bdev test'. Phases "
                          "run quick host suites first and all run even if one fails. Files it cannot map are listed "
                          "and skipped. The filters run are in the log. --plan lists them without running. A target (mac or android) limits the run to that platform's suites.",
                    examples=("bdev test-local", "bdev test-local android --device=emulator-5554", "bdev test-local mac --scope worktree")),
        CommandSpec("run", "Restart the browser with an existing output; never builds.", cmd_build.cmd_run,
                    positionals=(TARGET,), options=(CONFIGURATION, ARTIFACT, PLAN, DEVICE),
                    side_effects="macOS: quits any running instance of the same application (from any checkout), "
                                 "then launches the selected one. Android: installs the APK over the existing app on "
                                 "one device, stops that package there, and launches it. Profiles and app data are kept.",
                    notes="Older or independently built outputs may run; source state is not inspected.",
                    examples=("bdev run", "bdev run --artifact ./out/Custom/'Brave Browser Development.app'")),
        CommandSpec("deploy", "Install the Android build on a device and launch it (same as 'run android').",
                    cmd_build.cmd_deploy, positionals=(Positional("target", True, help="android"),),
                    options=(CONFIGURATION, ARTIFACT, PLAN, DEVICE),
                    side_effects="Installs the APK over the existing app on one device (data is kept), stops that "
                                 "package there, and launches it.",
                    examples=("bdev deploy android", "bdev deploy android --device emulator-5554")),
        CommandSpec("android setup", "Prepare shared Android-on-Mac support and link this workspace.",
                    android.cmd_android_setup, options=(SOURCE, REF),
                    side_effects="On macOS only: clones one shared support checkout and fetches its large files. "
                                 "Links the selected workspace, preserving existing copies. An explicit --ref "
                                 "changes the shared revision only when it has no local work; every linked checkout sees it.",
                    examples=("bdev android setup --checkout main", "bdev android setup --ref main")),
        CommandSpec("drift", "Compare patched Chromium files with the patch metadata (read-only).", cmd_patches.cmd_drift,
                    options=(DIFF,), examples=("bdev drift", "bdev drift --diff")),
        CommandSpec("patches update", "Generate Core patch changes from local Chromium edits.",
                    cmd_patches.cmd_patches_update, forward=True,
                    side_effects="Runs 'bpm run update_patches', which rewrites patch files in Core. Nothing is "
                                 "committed.", examples=("bdev patches update",)),
    ]
    registry = {}
    for spec in specs:
        registry[spec.name] = spec
        for alias in spec.aliases:
            registry[alias] = spec
    return registry


REGISTRY = build_registry()
