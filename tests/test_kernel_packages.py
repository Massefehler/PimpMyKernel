import os
import subprocess
import sys
import unittest
from support import GermanTestCase
from unittest.mock import patch

from pimpmykernel import privileged
from pimpmykernel.kernel_packages import (create_removal_plan, group_installed_kernels,
                                        parse_kernel_packages, plan_kernel_removal,
                                        simulate_removal, validate_apt_removal,
                                        validate_removal_packages)


RUNNING = "7.2.9-pmk"
GENERIC = "6.8.0-31-generic"
LOWLATENCY = "6.8.0-31-lowlatency"
SNAPSHOT = "\n".join(
    f"{name}\tinstalled\t1.0\t100" for name in (
        "linux-image-7.2.9-pmk", "linux-headers-7.2.9-pmk",
        "linux-image-7.2.9-pmk-dbg",
        "linux-image-6.8.0-31-generic", "linux-headers-6.8.0-31-generic",
        "linux-image-6.8.0-31-generic-dbgsym", "linux-modules-6.8.0-31-generic",
        "linux-modules-extra-6.8.0-31-generic", "linux-tools-6.8.0-31-generic",
        "linux-image-unsigned-6.8.0-31-lowlatency", "linux-headers-6.8.0-31-lowlatency",
        "linux-headers-6.8.0-31", "linux-tools-6.8.0-31",
    ))
PACKAGES = parse_kernel_packages(SNAPSHOT)


def apt_output(packages):
    return "Reading package lists...\n" + "\n".join(f"Remv {name} [1.0]" for name in packages)


class KernelPackageTests(GermanTestCase):
    def test_only_supported_installed_kernels_and_residuals_form_kernel_groups(self):
        extras = (
            "linux-image-generic\tinstalled\t1\t10\n"
            "linux-image-6.12-amd64\tinstalled\t1\t10\n"
            "linux-headers-generic-hwe-24.04\tinstalled\t1\t10\n"
            "linux-libc-dev:amd64\tinstalled\t1\t10\n"
            "linux-image-6.7.0-5-generic\tconfig-files\t1\t10\n"
            "linux-image-6.6.0-1-generic-dbg\tnot-installed\t1\t10\n"
            "linux-image-6.5.0-1-generic-dbg\tinstalled\t1\t10\n"
            "linux-headers-6.4.0-1\tinstalled\t1\t10\n"
            "malformed row\n"
        )
        kernels = group_installed_kernels(parse_kernel_packages(SNAPSHOT + "\n" + extras), RUNNING)
        self.assertEqual([kernel.release for kernel in kernels],
                         [RUNNING, LOWLATENCY, GENERIC, "6.7.0-5-generic"])
        self.assertFalse(kernels[-1].installed)
        self.assertTrue(kernels[0].active)
        self.assertIn("linux-image-7.2.9-pmk-dbg", kernels[0].packages)
        self.assertEqual(kernels[0].installed_bytes, 300 * 1024)
        generic = next(kernel for kernel in kernels if kernel.release == GENERIC)
        self.assertIn("linux-image-6.8.0-31-generic-dbgsym", generic.packages)
        self.assertIn("linux-headers-6.8.0-31", generic.packages)
        self.assertIn("linux-tools-6.8.0-31", generic.packages)

    def test_kernel_groups_use_natural_version_order(self):
        snapshot = parse_kernel_packages("\n".join(
            f"linux-image-{version}:amd64\tinstalled\t1\tbad-size"
            for version in ("7.2.9-pmk", "7.2.10-pmk", "6.8.0-9-generic", "6.8.0-31-generic")))
        kernels = group_installed_kernels(snapshot, "7.2.10-pmk")
        self.assertEqual([kernel.release for kernel in kernels],
                         ["7.2.10-pmk", "7.2.9-pmk", "6.8.0-31-generic", "6.8.0-9-generic"])
        self.assertEqual(kernels[0].installed_bytes, 0)

    def test_removed_kernel_modules_remain_available_for_configuration_cleanup(self):
        residual = "6.11.0-21-generic"
        snapshot = (*PACKAGES, *parse_kernel_packages("\n".join(
            f"{name}\tconfig-files\t1\t100" for name in (
                f"linux-modules-{residual}", f"linux-modules-extra-{residual}"))))
        kernels = {kernel.release: kernel for kernel in group_installed_kernels(snapshot, RUNNING)}
        self.assertFalse(kernels[residual].installed)
        self.assertEqual(kernels[residual].installed_bytes, 0)
        plan = create_removal_plan([residual], snapshot, RUNNING)
        self.assertEqual(plan.packages, (f"linux-modules-{residual}",
                                         f"linux-modules-extra-{residual}"))
        with patch("pimpmykernel.kernel_packages.query_kernel_packages", return_value=snapshot), \
                patch("pimpmykernel.kernel_packages.platform.release", return_value=RUNNING):
            self.assertEqual(validate_removal_packages(list(plan.packages)), plan)

    def test_residual_packages_are_included_with_their_installed_kernel(self):
        snapshot = (*PACKAGES, *parse_kernel_packages(
            f"linux-cloud-tools-{GENERIC}\tconfig-files\t1\t100"))
        plan = create_removal_plan([GENERIC], snapshot, RUNNING)
        self.assertIn(f"linux-cloud-tools-{GENERIC}", plan.packages)
        self.assertEqual(plan.installed_bytes, 6 * 100 * 1024)

    def test_residual_images_do_not_count_as_an_installed_fallback_kernel(self):
        snapshot = parse_kernel_packages(
            f"linux-image-{RUNNING}\tinstalled\t1\t100\n"
            f"linux-image-{GENERIC}\tconfig-files\t1\t100")
        with self.assertRaisesRegex(ValueError, "Mindestens"):
            create_removal_plan([RUNNING], snapshot, "external-kernel")
        self.assertEqual(create_removal_plan([GENERIC], snapshot, RUNNING).packages,
                         (f"linux-image-{GENERIC}",))

    def test_residuals_can_be_purged_without_any_installed_image_but_not_for_running_kernel(self):
        snapshot = parse_kernel_packages(
            f"linux-modules-{GENERIC}\tconfig-files\t1\t100")
        self.assertEqual(create_removal_plan([GENERIC], snapshot, "external-kernel").packages,
                         (f"linux-modules-{GENERIC}",))
        with self.assertRaisesRegex(ValueError, "laufende"):
            create_removal_plan([GENERIC], snapshot, GENERIC)

    def test_orphaned_shared_header_residuals_can_be_purged(self):
        for release in ("6.8.0-31", "6.12.111+deb13-common", "6.12.111+deb13-common-rt"):
            with self.subTest(release=release):
                name = f"linux-headers-{release}"
                snapshot = parse_kernel_packages(f"{name}\tconfig-files\t1\t100")
                kernels = group_installed_kernels(snapshot, "external-kernel")
                self.assertEqual(len(kernels), 1)
                self.assertEqual(kernels[0].packages, (name,))
                self.assertFalse(kernels[0].installed)
                self.assertEqual(create_removal_plan([release], snapshot, "external-kernel").packages,
                                 (name,))

    def test_shared_residual_headers_are_kept_for_an_installed_flavour(self):
        snapshot = parse_kernel_packages(
            f"linux-image-{GENERIC}\tconfig-files\t1\t100\n"
            f"linux-image-{LOWLATENCY}\tinstalled\t1\t100\n"
            "linux-headers-6.8.0-31\tconfig-files\t1\t100")
        self.assertEqual(create_removal_plan([GENERIC], snapshot, LOWLATENCY).packages,
                         (f"linux-image-{GENERIC}",))

    def test_shared_packages_are_kept_until_all_their_flavours_are_removed(self):
        plan = create_removal_plan([GENERIC], PACKAGES, RUNNING)
        self.assertIn("linux-modules-extra-6.8.0-31-generic", plan.packages)
        self.assertIn("linux-image-6.8.0-31-generic-dbgsym", plan.packages)
        self.assertNotIn("linux-headers-6.8.0-31", plan.packages)
        self.assertNotIn("linux-tools-6.8.0-31", plan.packages)
        self.assertFalse(any("lowlatency" in name or RUNNING in name for name in plan.packages))
        combined = create_removal_plan([GENERIC, LOWLATENCY], PACKAGES, RUNNING)
        self.assertIn("linux-headers-6.8.0-31", combined.packages)
        self.assertIn("linux-tools-6.8.0-31", combined.packages)
        self.assertEqual(combined.installed_bytes, len(combined.packages) * 100 * 1024)

    def test_debian_local_versions_unsigned_images_and_common_headers(self):
        core = "6.12.111+deb13"
        names = (f"linux-image-{core}-amd64", f"linux-image-{core}-cloud-amd64-unsigned",
                 f"linux-image-{core}-rt-amd64", f"linux-headers-{core}-amd64",
                 f"linux-headers-{core}-cloud-amd64", f"linux-headers-{core}-rt-amd64",
                 f"linux-headers-{core}-common", f"linux-headers-{core}-common-rt")
        snapshot = (*PACKAGES, *parse_kernel_packages("\n".join(
            f"{name}\tinstalled\t1\t100" for name in names)))
        running = core + "-cloud-amd64"
        kernels = {kernel.release: kernel for kernel in group_installed_kernels(snapshot, running)}
        self.assertTrue(kernels[running].active)
        self.assertIn(f"linux-image-{core}-cloud-amd64-unsigned", kernels[running].packages)
        self.assertIn(f"linux-headers-{core}-common", kernels[running].packages)
        self.assertNotIn(f"linux-headers-{core}-common-rt", kernels[running].packages)
        self.assertIn(f"linux-headers-{core}-common-rt", kernels[core + "-rt-amd64"].packages)
        self.assertNotIn(f"linux-headers-{core}-common", kernels[core + "-rt-amd64"].packages)
        plan = create_removal_plan([core + "-amd64"], snapshot, RUNNING)
        self.assertNotIn(f"linux-headers-{core}-common", plan.packages)
        combined = create_removal_plan([core + "-amd64", running], snapshot, RUNNING)
        self.assertIn(f"linux-headers-{core}-common", combined.packages)
        self.assertNotIn(f"linux-headers-{core}-common-rt", combined.packages)
        rt = create_removal_plan([core + "-rt-amd64"], snapshot, RUNNING)
        self.assertIn(f"linux-headers-{core}-common-rt", rt.packages)
        self.assertNotIn(f"linux-headers-{core}-common", rt.packages)
        with self.assertRaisesRegex(ValueError, "laufende"):
            create_removal_plan([running], snapshot, running)

    def test_running_last_missing_and_empty_selections_are_rejected(self):
        for selected, running, error in (
                ([RUNNING], RUNNING, "laufende"),
                ([RUNNING, GENERIC, LOWLATENCY], "external-kernel", "Mindestens"),
                (["6.7.0-1-generic"], RUNNING, "aktuell"),
                ([], RUNNING, "aktuell")):
            with self.subTest(selected=selected), self.assertRaisesRegex(ValueError, error):
                create_removal_plan(selected, PACKAGES, running)

    def test_apt_instruction_suffix_in_package_name_is_rejected(self):
        extra = parse_kernel_packages("linux-image-6.9.0-custom+\tinstalled\t1\t1")
        with self.assertRaisesRegex(ValueError, "Paketnamen"):
            create_removal_plan(["6.9.0-custom+"], (*PACKAGES, *extra), RUNNING)

    def test_root_requires_the_complete_fresh_kernel_package_list(self):
        plan = create_removal_plan([GENERIC], PACKAGES, RUNNING)
        with patch("pimpmykernel.kernel_packages.query_kernel_packages", return_value=PACKAGES), \
                patch("pimpmykernel.kernel_packages.platform.release", return_value=RUNNING):
            self.assertEqual(validate_removal_packages(list(plan.packages)), plan)
            for arguments in ([], ["--help"], ["linux-image-generic"],
                              ["linux-libc-dev"], [plan.packages[0]] * 2,
                              list(plan.packages[:-1]),
                              ["linux-image-7.2.9-pmk", "linux-headers-7.2.9-pmk",
                               "linux-image-7.2.9-pmk-dbg"]):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    validate_removal_packages(arguments)

    def test_root_rechecks_a_changed_running_kernel(self):
        plan = create_removal_plan([GENERIC], PACKAGES, RUNNING)
        with patch("pimpmykernel.kernel_packages.query_kernel_packages", return_value=PACKAGES), \
                patch("pimpmykernel.kernel_packages.platform.release", return_value=GENERIC), \
                self.assertRaisesRegex(ValueError, "laufende"):
            validate_removal_packages(list(plan.packages))

    def test_root_rejects_packages_changed_since_preview(self):
        plan = create_removal_plan([GENERIC], PACKAGES, RUNNING)
        changed = (*PACKAGES, *parse_kernel_packages(
            "linux-cloud-tools-6.8.0-31-generic\tinstalled\t1\t1"))
        with patch("pimpmykernel.kernel_packages.query_kernel_packages", return_value=changed), \
                patch("pimpmykernel.kernel_packages.platform.release", return_value=RUNNING), \
                self.assertRaisesRegex(ValueError, "Paketliste"):
            validate_removal_packages(list(plan.packages))

    def test_apt_simulation_must_remove_exactly_the_expected_packages(self):
        plan = create_removal_plan([GENERIC], PACKAGES, RUNNING)
        validate_apt_removal(apt_output(plan.packages), plan.packages)
        validate_apt_removal(apt_output(plan.packages).replace("Remv ", "Purg "), plan.packages)
        validate_apt_removal(apt_output(tuple(name + ":amd64" for name in plan.packages)), plan.packages)
        for output, error in (
                (apt_output(plan.packages) + "\nRemv linux-image-generic-hwe-24.04 [1.0]", "weitere Pakete"),
                (apt_output(plan.packages).replace("Remv ", "Purg ") +
                 "\nPurg linux-image-generic-hwe-24.04 [1.0]", "weitere Pakete"),
                (apt_output(plan.packages[:-1]), "Auswahl"),
                (apt_output(plan.packages) + "\nInst linux-image-new [1.0]", "installieren"),
                (apt_output(plan.packages) + "\nConf linux-image-new [1.0]", "einrichten")):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                validate_apt_removal(output, plan.packages)

    def test_preview_uses_only_read_only_apt_simulation_and_no_auto_removal(self):
        plan = create_removal_plan([GENERIC], PACKAGES, RUNNING)
        response = subprocess.CompletedProcess([], 0, apt_output(plan.packages), "")
        with patch("pimpmykernel.kernel_packages.query_kernel_packages", return_value=PACKAGES), \
                patch("pimpmykernel.kernel_packages.platform.release", return_value=RUNNING), \
                patch("pimpmykernel.kernel_packages.subprocess.run", return_value=response) as run:
            self.assertEqual(plan_kernel_removal([GENERIC]), plan)
        self.assertEqual(run.call_args.args[0],
                         ["/usr/bin/apt-get", "--simulate", "--no-auto-remove", "purge", "--", *plan.packages])
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")

    def test_failed_apt_simulation_is_reported(self):
        plan = create_removal_plan([GENERIC], PACKAGES, RUNNING)
        response = subprocess.CompletedProcess([], 100, "", "dependency conflict")
        with patch("pimpmykernel.kernel_packages.subprocess.run", return_value=response), \
                self.assertRaisesRegex(ValueError, "dependency conflict"):
            simulate_removal(plan)


class PrivilegedRemovalTests(GermanTestCase):
    def run_helper(self, plan, simulation_error=None, *, packages=PACKAGES):
        with patch.dict(os.environ), patch.object(sys, "path", sys.path.copy()), \
                patch.object(sys, "argv", ["privileged.py", "remove", *plan.packages]), \
                patch("pimpmykernel.privileged.os.geteuid", return_value=0), \
                patch("pimpmykernel.privileged.os.umask"), \
                patch("pimpmykernel.kernel_packages.query_kernel_packages", return_value=packages), \
                patch("pimpmykernel.kernel_packages.platform.release", return_value=RUNNING), \
                patch("pimpmykernel.kernel_packages.simulate_removal", side_effect=simulation_error) as simulate, \
                patch("pimpmykernel.privileged.subprocess.call", return_value=0) as call:
            if simulation_error:
                with self.assertRaisesRegex(ValueError, "weitere Pakete"):
                    privileged.main()
            else:
                self.assertEqual(privileged.main(), 0)
            return simulate, call

    def test_confirmed_removal_rechecks_packages_and_dependencies_before_apt(self):
        plan = create_removal_plan([GENERIC], PACKAGES, RUNNING)
        simulate, call = self.run_helper(plan)
        simulate.assert_called_once_with(plan)
        call.assert_called_once_with(["/usr/bin/apt-get", "--yes", "--no-auto-remove",
                                      "purge", "--", *plan.packages])

    def test_changed_dependencies_prevent_actual_removal(self):
        plan = create_removal_plan([GENERIC], PACKAGES, RUNNING)
        simulate, call = self.run_helper(plan, ValueError("APT würde weitere Pakete entfernen"))
        simulate.assert_called_once_with(plan)
        call.assert_not_called()

    def test_confirmed_module_residual_cleanup_is_revalidated_and_purged(self):
        residual = "6.11.0-21-generic"
        snapshot = (*PACKAGES, *parse_kernel_packages(
            f"linux-modules-{residual}\tconfig-files\t1\t100"))
        plan = create_removal_plan([residual], snapshot, RUNNING)
        simulate, call = self.run_helper(plan, packages=snapshot)
        simulate.assert_called_once_with(plan)
        call.assert_called_once_with(["/usr/bin/apt-get", "--yes", "--no-auto-remove",
                                      "purge", "--", *plan.packages])


if __name__ == "__main__":
    unittest.main()
