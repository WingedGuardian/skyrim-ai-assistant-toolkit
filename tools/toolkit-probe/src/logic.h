#pragma once
#include <algorithm>
#include <cctype>
#include <cmath>
#include <string>
#include <vector>
namespace probe {
struct V {
  double x, y, z;
};
inline double sphereDistance(V a, V b, double r) {
  return (std::max)(0.0, std::sqrt((a.x - b.x) * (a.x - b.x) +
                                   (a.y - b.y) * (a.y - b.y) +
                                   (a.z - b.z) * (a.z - b.z)) -
                             r);
}
inline double raySphere(V o, V d, V c, double r, double max) {
  V v{o.x - c.x, o.y - c.y, o.z - c.z};
  double b = v.x * d.x + v.y * d.y + v.z * d.z;
  double q = v.x * v.x + v.y * v.y + v.z * v.z - r * r;
  if (q <= 0)
    return 0;
  double disc = b * b - q;
  if (disc < 0)
    return -1;
  double t = -b - std::sqrt(disc);
  return t >= 0 && t <= max ? t : -1;
}
inline bool validForm(const std::string &s) {
  return !s.empty() && s.size() <= 8 &&
         std::all_of(s.begin(), s.end(),
                     [](unsigned char c) { return std::isxdigit(c) != 0; });
}
inline double chooseDistance(const std::vector<double> &values) {
  double best = -1;
  for (double d : values)
    if (d >= 0 && (best < 0 || d < best))
      best = d;
  return best;
}
struct Delta {
  std::vector<std::string> added, removed;
};
inline std::string captureStatus(unsigned gen, unsigned current, bool stopped,
                                 const std::string &reason, double now,
                                 double expiry) {
  return gen != current  ? "invalidated"
         : stopped       ? reason
         : now >= expiry ? "expired"
                         : "active";
}
inline bool mayRun(unsigned request, unsigned current, double now,
                   double deadline) {
  return request == current && now < deadline;
}
} // namespace probe
