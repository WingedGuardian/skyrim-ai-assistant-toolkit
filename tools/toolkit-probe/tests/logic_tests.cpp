#include "logic.h"
#include <iostream>
int main() {
  int failures = 0, checks = 0;
  auto check = [&](bool v, const char *s) {
    ++checks;
    if (!v) {
      std::cerr << s << "\n";
      ++failures;
    }
  };
  check(probe::raySphere({0, 0, 0}, {0, 1, 0}, {0, 20, 0}, 5, 100) == 15,
        "ray entry must use near surface");
  check(probe::raySphere({0, 0, 0}, {0, 1, 0}, {20, 0, 0}, 5, 100) < 0,
        "off-ray sphere excluded");
  check(probe::raySphere({0, 0, 0}, {0, 1, 0}, {0, 0, 0}, 5, 100) == 0,
        "inside bounds entry zero");
  check(probe::sphereDistance({0, 0, 0}, {0, 780, 0}, 790) == 0,
        "distant origin reaching player retained");
  check(probe::validForm("00000014") && !probe::validForm("14junk") &&
            !probe::validForm("-1"),
        "form rejects partial parsing");
  check(!probe::mayRun(2, 3, 0, 10) && !probe::mayRun(2, 2, 10, 10),
        "expired and stale task cannot dereference");
  check(probe::chooseDistance({30., 5., -1.}) == 5.,
        "shape closest entry outranks root entry");
  check(probe::captureStatus(2, 2, true, "superseded", 5, 10) == "superseded" &&
            probe::captureStatus(2, 3, true, "stopped", 5, 10) ==
                "invalidated" &&
            probe::captureStatus(2, 2, false, "stopped", 10, 10) == "expired",
        "capture terminal status preserves supersession and invalidation");
  std::cout << checks << " explicit checks, " << failures << " failures\n";
  return failures ? 1 : 0;
}
