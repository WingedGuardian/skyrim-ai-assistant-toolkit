#pragma once
#include "logic.h"
#include <deque>
#include <limits>
#include <nlohmann/json.hpp>
#include <stdexcept>
#include <unordered_set>
namespace probe {
using Json = nlohmann::json;
// Copy assigned paths independently of loaded objects: a null or replacement
// texture must not hide the material's request. Neither observation proves a
// GPU binding, nor does an absent object establish a missing asset.
inline Json effectTextures(const char *sourcePath, const char *greyscalePath,
                           const char *sourceName, const char *greyscaleName) {
  Json textures = Json::array();
  auto add = [&](const char *slot, const char *path, const char *source) {
    if (path && *path)
      textures.push_back({{"slot", slot}, {"path", path}, {"source", source}});
  };
  add("source", sourcePath, "runtime effect material path");
  add("greyscale", greyscalePath, "runtime effect material path");
  add("source", sourceName, "loaded source texture");
  add("greyscale", greyscaleName, "loaded source texture");
  return textures;
}
inline unsigned long long integer(const Json &a, const char *key,
                                  unsigned long long fallback,
                                  unsigned long long min,
                                  unsigned long long max) {
  if (!a.contains(key))
    return fallback;
  auto &v = a.at(key);
  if (!v.is_number_integer())
    throw std::runtime_error(std::string(key) +
                             " must be an integer JSON value");
  if (v.is_number_unsigned()) {
    auto n = v.get<unsigned long long>();
    if (n < min || n > max)
      throw std::runtime_error(std::string(key) + " outside caps");
    return n;
  }
  auto n = v.get<long long>();
  if (n < 0 || static_cast<unsigned long long>(n) < min ||
      static_cast<unsigned long long>(n) > max)
    throw std::runtime_error(std::string(key) + " outside caps");
  return static_cast<unsigned long long>(n);
}
inline Json validate(Json a, const std::string &op, bool vr) {
  if (!a.is_object())
    throw std::runtime_error("arguments must be object");
  if (a.contains("seconds"))
    a["seconds"] = integer(a, "seconds", 180, 1, 600);
  if (a.contains("limit"))
    a["limit"] = integer(a, "limit", 20, 1, 100);
  if (a.contains("after"))
    a["after"] = integer(a, "after", 0, 0, 9007199254740991ULL);
  for (auto key : {"read", "stop"})
    if (a.contains(key) && !a[key].is_boolean())
      throw std::runtime_error(std::string(key) + " must be boolean");
  for (auto key : {"actor", "formId", "captureId", "types"})
    if (a.contains(key) && !a[key].is_string())
      throw std::runtime_error(std::string(key) + " must be string");
  for (auto key : {"max", "radius"})
    if (a.contains(key)) {
      if (!a[key].is_number())
        throw std::runtime_error(std::string(key) + " must be numeric");
      auto n = a[key].get<double>();
      if (!std::isfinite(n) || n <= 0 || n > 10000)
        throw std::runtime_error(std::string(key) + " outside caps");
    }
  if (a.contains("types") && a["types"].get<std::string>().empty())
    a.erase("types");
  if (a.contains("at")) {
    auto &at = a["at"];
    if (at.is_string()) {
      if (at != "player")
        throw std::runtime_error("at string must be player");
    } else {
      if (!at.is_array() || at.size() != 3)
        throw std::runtime_error("at needs finite xyz");
      for (auto &n : at) {
        if (!n.is_number() || !std::isfinite(n.get<double>()) ||
            std::abs(n.get<double>()) > 100000000)
          throw std::runtime_error(
              "at xyz outside finite world coordinate caps");
      }
    }
  }
  if (op == "look") {
    if (a.contains("from") && !a["from"].is_string())
      throw std::runtime_error("from must be a string");
    auto pose = a.value("from", vr ? "hmd" : "crosshair");
    if (pose != "hmd" && pose != "right" && pose != "left" &&
        pose != "crosshair")
      throw std::runtime_error("unknown pose");
    if (!vr && pose != "crosshair")
      throw std::runtime_error("pose unsupported on this runtime");
    a["from"] = pose;
  }
  return a;
}
struct RecipientBinding {
  std::string id;
  unsigned generation;
};
inline bool sameRecipient(const RecipientBinding &accepted,
                          const std::string &current, unsigned generation) {
  return accepted.id == current && accepted.generation == generation;
}
template <class Lookup, class Deliver>
void deliverAccepted(const std::vector<RecipientBinding> &accepted,
                     unsigned currentGeneration, Lookup lookup,
                     Deliver deliver) {
  for (auto &binding : accepted) {
    if (binding.generation != currentGeneration)
      continue;
    auto *capture = lookup(binding.id);
    if (capture && sameRecipient(binding, capture->id, capture->gen))
      deliver(*capture);
  }
}
struct PendingBatch {
  std::vector<RecipientBinding> recipients;
  bool admitted = false, settled = false;
};
template <class Lookup> void markPending(PendingBatch &batch, Lookup lookup) {
  if (batch.admitted)
    return;
  for (auto &binding : batch.recipients) {
    auto *capture = lookup(binding.id);
    if (capture && sameRecipient(binding, capture->id, capture->gen))
      ++capture->pendingEvents;
  }
  batch.admitted = true;
}
template <class Lookup> bool settlePending(PendingBatch &batch, Lookup lookup) {
  if (!batch.admitted || batch.settled)
    return false;
  for (auto &binding : batch.recipients) {
    auto *capture = lookup(binding.id);
    if (capture && sameRecipient(binding, capture->id, capture->gen) &&
        capture->pendingEvents)
      --capture->pendingEvents;
  }
  batch.settled = true;
  return true;
}
struct RegistrationGate {
  bool attempted = false;
  bool ready(bool dataLoaded) {
    if (!dataLoaded || attempted)
      return false;
    attempted = true;
    return true;
  }
};
struct VisualBaseline {
  bool established = false;
  std::unordered_set<std::string> prior;
  Delta observe(const std::vector<std::string> &ids, bool complete) {
    if (!complete)
      return {};
    std::unordered_set<std::string> current(ids.begin(), ids.end());
    Delta d;
    if (established) {
      for (auto &id : ids)
        if (!prior.contains(id))
          d.added.push_back(id);
      for (auto &id : prior)
        if (!current.contains(id))
          d.removed.push_back(id);
    }
    prior = std::move(current);
    established = true;
    return d;
  }
};
inline std::string safeDump(const Json &j) {
  return j.dump(-1, ' ', false, Json::error_handler_t::replace);
}
inline constexpr const char *fallbackResponse =
    R"({"schemaVersion":1,"ok":false,"source":"ToolkitProbe","runtime":"unknown","sessionId":null,"sessionGeneration":null,"generation":null,"observedAt":null,"coverage":{"status":"failed","reason":"response serialization failed"},"truncated":true,"timings":{"budgetMs":5,"mainThreadMs":null},"encoding":"utf8-replacement","error":{"code":"serialization_failed","message":"native response failed"}})";
template <class Build, class Write>
void emit(Build build, Write write) noexcept {
  std::string output;
  const char *response = fallbackResponse;
  try {
    output = build();
    response = output.c_str();
  } catch (...) {
  }
  try {
    write(response);
  } catch (...) {
  }
}
template <class T> struct PagedRing {
  struct Entry {
    unsigned long long sequence;
    T value;
  };
  struct Page {
    bool lost = false;
    std::vector<Entry> events;
    unsigned long long nextSequence = 0;
    bool hasMore = false;
  };
  unsigned cap;
  unsigned long long next = 1;
  std::deque<Entry> events;
  explicit PagedRing(unsigned c) : cap(c) {
    if (!c)
      throw std::runtime_error("zero ring capacity");
  }
  void push(T value) {
    if (events.size() == cap)
      events.pop_front();
    events.push_back({next++, std::move(value)});
  }
  template <class Ready>
  Page read(unsigned long long after, unsigned max, Ready ready) const {
    Page p;
    p.lost = !events.empty() && after < events.front().sequence - 1;
    p.nextSequence = after;
    if (events.empty() || after >= events.back().sequence)
      return p;
    size_t begin =
        after < events.front().sequence
            ? 0
            : static_cast<size_t>(after - events.front().sequence + 1);
    for (size_t i = begin; i < events.size(); ++i) {
      auto &e = events[i];
      if (p.events.size() >= max || !ready()) {
        p.hasMore = true;
        break;
      }
      p.events.push_back(e);
      p.nextSequence = e.sequence;
    }
    return p;
  }
};
} // namespace probe
