#include "contract.h"
#include "watch_queue.h"
#include <iostream>
#include <limits>
#include <map>
int main() {
  int failures = 0, checks = 0;
  auto check = [&](bool ok, const char *msg) {
    ++checks;
    if (!ok) {
      std::cerr << msg << "\n";
      ++failures;
    }
  };
  auto requestedOnly = probe::effectTextures(
      "textures/missing-source.dds", "textures/missing-grey.dds", nullptr, nullptr);
  check(requestedOnly == probe::Json::array({
            {{"slot", "source"}, {"path", "textures/missing-source.dds"},
             {"source", "runtime effect material path"}},
            {{"slot", "greyscale"}, {"path", "textures/missing-grey.dds"},
             {"source", "runtime effect material path"}}}),
        "effect assignments survive null loaded objects without absence inference");
  auto replaced = probe::effectTextures(
      "textures/requested.dds", "textures/requested-grey.dds",
      "textures/fallback.dds", "textures/loaded-grey.dds");
  check(replaced == probe::Json::array({
            {{"slot", "source"}, {"path", "textures/requested.dds"},
             {"source", "runtime effect material path"}},
            {{"slot", "greyscale"}, {"path", "textures/requested-grey.dds"},
             {"source", "runtime effect material path"}},
            {{"slot", "source"}, {"path", "textures/fallback.dds"},
             {"source", "loaded source texture"}},
            {{"slot", "greyscale"}, {"path", "textures/loaded-grey.dds"},
             {"source", "loaded source texture"}}}),
        "effect assigned and loaded names remain independent on disagreement");
  auto loadedOnly = probe::effectTextures(nullptr, "", "textures/loaded.dds", "");
  check(loadedOnly == probe::Json::array({
            {{"slot", "source"}, {"path", "textures/loaded.dds"},
             {"source", "loaded source texture"}}}) &&
            probe::effectTextures(nullptr, "", "", nullptr).empty(),
        "effect empty assignments and loaded names do not invent texture paths");
  auto rejects = [&](const probe::Json &a, const char *op, bool vr = true) {
    try {
      probe::validate(a, op, vr);
      return false;
    } catch (...) {
      return true;
    }
  };
  check(rejects({{"seconds", 1.5}}, "watch") &&
            rejects({{"seconds", -1}}, "watch") &&
            rejects({{"seconds", 18446744073709551615ULL}}, "watch"),
        "seconds reject fractional negative and overflow before conversion");
  check(rejects({{"limit", 1.1}}, "near") &&
            rejects({{"limit", 4294967316ULL}}, "near") &&
            rejects({{"after", -1}}, "watch") &&
            rejects({{"after", 9007199254740992ULL}}, "watch"),
        "limit and cursor reject fractional wrap and huge values");
  check(rejects({{"at", "somewhere"}}, "near") &&
            rejects({{"at", {0, 1, std::numeric_limits<double>::infinity()}}},
                    "near") &&
            rejects({{"at", {0, 1, 1e20}}}, "near"),
        "near point requires player or finite bounded xyz");
  check(rejects({{"from", "right"}}, "look", false) &&
            rejects({{"from", "hmd"}}, "look", false) &&
            rejects({{"from", "wrong"}}, "look"),
        "unsupported runtime poses are rejected");
  auto normal = probe::validate({{"types", ""}}, "near", true);
  check(!normal.contains("types"), "empty type filter is omitted");
  probe::RecipientBinding accepted{"capture-old", 2};
  check(
      probe::sameRecipient(accepted, "capture-old", 2) &&
          !probe::sameRecipient(accepted, "capture-new", 2) &&
          !probe::sameRecipient(accepted, "capture-old", 3),
      "enqueue restart deliver stays bound to original capture and generation");
  struct FakeCapture {
    std::string id;
    unsigned gen;
    bool stopped = false;
    std::vector<std::string> events;
  };
  std::map<std::string, FakeCapture> registry;
  registry.emplace("old", FakeCapture{"old", 2});
  std::vector<probe::RecipientBinding> queueRecipients{{"old", 2}};
  auto deliver = [&] {
    probe::deliverAccepted(
        queueRecipients, 2,
        [&](const std::string &id) -> FakeCapture * {
          auto it = registry.find(id);
          return it == registry.end() ? nullptr : &it->second;
        },
        [&](FakeCapture &c) { c.events.push_back("accepted before restart"); });
  };
  registry.at("old").stopped = true;
  registry.emplace("new", FakeCapture{"new", 2});
  deliver();
  check(registry.at("old").events ==
                std::vector<std::string>{"accepted before restart"} &&
            registry.at("new").events.empty(),
        "enqueue restart deliver cannot contaminate replacement capture");
  struct PendingCapture {
    std::string id;
    unsigned gen;
    unsigned pendingEvents = 0;
    bool stopped = false;
    std::vector<std::string> events;
  };
  std::map<std::string, PendingCapture> pendingRegistry;
  pendingRegistry.emplace("owned", PendingCapture{"owned", 2});
  auto findPending = [&](const std::string &id) -> PendingCapture * {
    auto it = pendingRegistry.find(id);
    return it == pendingRegistry.end() ? nullptr : &it->second;
  };
  probe::PendingBatch batch;
  batch.recipients.push_back({"owned", 2});
  probe::markPending(batch, findPending);
  auto finishQueued = [&] {
    probe::deliverAccepted(
        batch.recipients, 2, findPending,
        [&](PendingCapture &c) { c.events.push_back("event"); });
    probe::settlePending(batch, findPending);
  };
  pendingRegistry.at("owned").stopped = true;
  check(pendingRegistry.at("owned").pendingEvents == 1 &&
            pendingRegistry.at("owned").events.empty(),
        "enqueue stop read reports pending before queued delivery");
  finishQueued();
  probe::settlePending(batch, findPending);
  check(pendingRegistry.at("owned").pendingEvents == 0 &&
            pendingRegistry.at("owned").events ==
                std::vector<std::string>{"event"},
        "delivery settles once and terminal read exposes accepted event");
  probe::PendingBatch aborted;
  aborted.recipients.push_back({"owned", 2});
  probe::markPending(aborted, findPending);
  probe::deliverAccepted(aborted.recipients, 3, findPending,
                         [&](PendingCapture &c) {
                           c.events.push_back("must not deliver across load");
                         });
  probe::settlePending(aborted, findPending);
  check(pendingRegistry.at("owned").pendingEvents == 0 &&
            pendingRegistry.at("owned").events.size() == 1,
        "abort after invalidation also settles admitted recipient");
  probe::RegistrationGate registration;
  check(
      !registration.ready(false) && !registration.ready(false),
      "registration never requests interface during plugin post-load startup");
  check(registration.ready(true) && !registration.ready(true),
        "data-loaded host-ready registration is attempted exactly once");
  probe::VisualBaseline baseline;
  auto d1 = baseline.observe({"a"}, false);
  auto d2 = baseline.observe({"a", "b"}, true);
  auto d3 = baseline.observe({}, false);
  auto d4 = baseline.observe({"b", "c"}, true);
  check(d1.added.empty() && d2.added.empty() && d3.removed.empty() &&
            d4.added == std::vector<std::string>{"c"} &&
            d4.removed == std::vector<std::string>{"a"},
        "first complete snapshot establishes baseline and unavailable cannot "
        "remove");
  int writes = 0;
  std::string output;
  probe::emit(
      [&] {
        return probe::safeDump(
            probe::Json{{"name", std::string("bad\xff", 4)}});
      },
      [&](const char *s) {
        ++writes;
        output = s;
      });
  check(writes == 1 && probe::Json::parse(output)["name"].get<std::string>() ==
                           std::string("bad\xef\xbf\xbd", 6),
        "malformed engine UTF8 safely replaces and writes once");
  writes = 0;
  probe::emit(
      []() -> std::string { throw std::runtime_error("serialize failure"); },
      [&](const char *s) {
        ++writes;
        output = s;
      });
  check(writes == 1 && !probe::Json::parse(output)["ok"].get<bool>(),
        "serialization exception emits ASCII fallback exactly once");
  writes = 0;
  probe::emit([] { return std::string("{}"); },
              [&](const char *) {
                ++writes;
                throw std::runtime_error("host writer failed");
              });
  check(writes == 1, "throwing writer is never retried or leaked");
  probe::PagedRing<int> ring(3);
  ring.push(1);
  ring.push(2);
  ring.push(3);
  ring.push(4);
  auto p1 = ring.read(0, 2, [] { return true; });
  auto p2 = ring.read(p1.nextSequence, 2, [] { return true; });
  check(p1.lost && p1.events.size() == 2 && p1.nextSequence == 3 &&
            p1.hasMore && p2.events.size() == 1 && p2.events[0].value == 4 &&
            !p2.hasMore,
        "paged ring cursor cannot skip unreturned events");
  auto p3 = ring.read(1, 2, [] { return false; });
  check(p3.events.empty() && p3.nextSequence == 1 && p3.hasMore,
        "budget exhaustion preserves unread cursor");

  // These use the same admission/drain/ticket helper as the engine adapter.
  struct QueueCapture {
    std::string id;
    unsigned gen = 1;
    unsigned long long pendingEvents = 0, eventQueueDropped = 0,
                       eventFailures = 0, queueHighWaterMark = 0,
                       drainBudgetStops = 0;
    double maxDeliveryDelayMs = 0, maxDrainMs = 0;
    bool stopped = false;
    std::vector<int> delivered;
    unsigned samples = 0;
  };
  struct QueueEvent {
    probe::PendingBatch pending;
    int value;
    double time, enqueuedMs;
  };
  std::map<std::string, QueueCapture> qc;
  qc.emplace("a", QueueCapture{"a"});
  qc.emplace("b", QueueCapture{"b"});
  auto qfind = [&](const std::string &id) -> QueueCapture * {
    auto it = qc.find(id);
    return it == qc.end() ? nullptr : &it->second;
  };
  probe::WatchQueue<QueueEvent> q;
  double ms = 100;
  auto enqueue = [&](int value, const std::string &id = "a", unsigned gen = 1) {
    QueueEvent e{{{{id, gen}}}, value, 123.0 + value, ms};
    return q.enqueue(std::move(e), qfind);
  };
  auto qdrain = [&](auto ready) {
    return q.drain(1, qfind, ready,
      [&](QueueCapture &c, QueueEvent &e) {
        c.delivered.push_back(e.value);
        check(e.time == 123.0 + e.value, "callback timestamps stay immutable");
      },
      [&](QueueCapture &c, QueueEvent &) { if (!c.stopped) ++c.samples; },
      [&] { return ms; });
  };
  for (int i = 0; i < 125; ++i) enqueue(i, i % 2 ? "b" : "a");
  auto ticket = q.claimDispatch();
  check(ticket && !q.claimDispatch() && q.owned() == 125 &&
            qc.at("a").pendingEvents == 63,
        "burst beyond four retains all records and admits one dispatcher");
  ms = 112;
  auto firstDrain = qdrain([] { return true; });
  check(firstDrain.records == 50 && q.owned() == 75 &&
            qc.at("a").samples == 1 && qc.at("b").samples == 1 &&
            qc.at("a").maxDeliveryDelayMs == 12 &&
            !q.claimDispatch(),
        "bounded read drain coalesces each actor and preserves outstanding ticket");
  q.finishDispatch(ticket);
  check(q.outstanding() == 0 && q.owned() == 75,
        "finishing drain does not schedule its own successor");
  unsigned permits = 7;
  auto partial = qdrain([&] { return permits-- > 0; });
  check(partial.records == 7 && partial.budgetStopped && q.owned() == 68 &&
            qc.at("a").drainBudgetStops && qc.at("b").drainBudgetStops,
        "partial cooperative budget retains remainder and records budget stops");
  while (q.owned()) qdrain([] { return true; });
  bool ordered = true;
  for (auto &[id, c] : qc) {
    for (size_t i = 1; i < c.delivered.size(); ++i)
      ordered = ordered && c.delivered[i] == c.delivered[i - 1] + 2;
  }
  check(ordered && qc.at("a").delivered.size() == 63 &&
            qc.at("b").delivered.size() == 62 &&
            !qc.at("a").pendingEvents && !qc.at("b").pendingEvents,
        "all admitted burst events deliver in order and settle exactly once");
  for (unsigned i = 0; i < 2000; ++i) enqueue(i);
  check(!enqueue(2000) && q.owned() == 2000 &&
            qc.at("a").eventQueueDropped == 1 &&
            qc.at("a").queueHighWaterMark == 2000,
        "capacity includes all owned records and overflow drops new visibly");
  bool inFlightRejected = false;
  auto flight = q.drain(1, qfind, [] { return true; },
    [&](QueueCapture &, QueueEvent &) {
      if (!inFlightRejected) {
        inFlightRejected = !enqueue(2001);
        check(q.owned() == 2000, "popped in-flight record still counts at capacity");
      }
    }, [](QueueCapture &, QueueEvent &) {}, [&] { return ms; });
  check(inFlightRejected && flight.records == 50 &&
            qc.at("a").eventQueueDropped == 2,
        "reentrant admission cannot exceed capacity while a record is in flight");
  q.discard(qfind, false);
  check(!q.owned() && !qc.at("a").pendingEvents,
        "load invalidation discards and settles backlog without another trigger");
  enqueue(30);
  auto failedTicket = q.claimDispatch();
  q.failDispatch(failedTicket, qfind);
  q.failDispatch(failedTicket, qfind);
  check(!q.owned() && !qc.at("a").pendingEvents &&
            qc.at("a").eventFailures == 1 && !q.outstanding(),
        "failed or unrun disposed dispatcher settles admissions exactly once");
  enqueue(40);
  auto oldTicket = q.claimDispatch();
  qdrain([] { return true; });
  check(!q.claimDispatch(), "read emptying queue cannot release queued ticket");
  q.finishDispatch(oldTicket);
  enqueue(41);
  auto newTicket = q.claimDispatch();
  q.failDispatch(oldTicket, qfind);
  check(q.outstanding() == newTicket && q.owned() == 1,
        "late disposal cannot abort a replacement dispatcher or its work");
  q.finishDispatch(newTicket);
  qc.at("a").stopped = true;
  unsigned beforeSamples = qc.at("a").samples;
  qdrain([] { return true; });
  check(qc.at("a").delivered.back() == 41 &&
            qc.at("a").samples == beforeSamples,
        "terminal capture receives accepted direct event but no stopped visuals");
  enqueue(42);
  qc.at("a").stopped = true;  // superseded original, replacement has another ID
  qc.emplace("replacement", QueueCapture{"replacement"});
  qdrain([] { return true; });
  check(qc.at("a").delivered.back() == 42 &&
            qc.at("replacement").delivered.empty(),
        "supersession does not redirect queued recipients");
  enqueue(43);
  unsigned derefs = 0;
  q.drain(2, qfind, [] { return true; },
    [&](QueueCapture &, QueueEvent &) { ++derefs; },
    [&](QueueCapture &, QueueEvent &) { ++derefs; }, [&] { return ms; });
  check(!derefs && !qc.at("a").pendingEvents && !q.owned(),
        "stale generation settles without source or actor dereference");
  enqueue(44);
  q.drain(1, qfind, [] { return true; },
    [&](QueueCapture &, QueueEvent &) { throw std::runtime_error("row encoding"); },
    [&](QueueCapture &, QueueEvent &) { ++derefs; }, [&] { return ms; });
  check(qc.at("a").eventFailures == 2 && !q.owned() && !derefs,
        "row publication exception visibly fails and settles without visual work");
  enqueue(45);
  q.drain(1, qfind, [] { return true; },
    [&](QueueCapture &, QueueEvent &) {},
    [&](QueueCapture &, QueueEvent &) { throw std::runtime_error("visual scan"); },
    [&] { return ms; });
  check(qc.at("a").eventFailures == 3 && !q.owned() &&
            !qc.at("a").pendingEvents,
        "visual exception cannot leak admission or replay direct event");
  probe::PagedRing<int> fullRing(2000);
  for (int i = 0; i < 2000; ++i) fullRing.push(i);
  auto savedPage = fullRing.read(0, 50, [] { return true; });
  enqueue(46);
  q.drain(1, qfind, [] { return true; },
    [&](QueueCapture &, QueueEvent &) { fullRing.push(2000); },
    [](QueueCapture &, QueueEvent &) {}, [&] { return ms; });
  auto followingPage = fullRing.read(savedPage.nextSequence, 50,
                                    [] { return true; });
  check(savedPage.events.front().sequence == 1 && savedPage.nextSequence == 50 &&
            !savedPage.lost && !followingPage.lost &&
            fullRing.read(0, 50, [] { return true; }).lost,
        "page-before-drain preserves owned unread rows despite legitimate eviction");

  // Exercise the actual delegate lease used by the SKSE adapter, including
  // disposal, failed submissions, and ambiguous submit-then-throw behavior.
  std::function<void()> queuedDispatch;
  auto dispose = [&](std::uint64_t t) { q.failDispatch(t, qfind); };
  auto run = [&](std::uint64_t t) {
    if (q.outstanding() != t) return;
    qdrain([] { return true; });
    q.finishDispatch(t);
  };
  auto submit = [&](auto task) { queuedDispatch = std::move(task); };
  auto beforeFailures = qc.at("a").eventFailures;
  enqueue(60);
  auto disposalTicket = q.claimDispatch();
  probe::submitWatchDispatch(disposalTicket, submit, run, dispose);
  queuedDispatch = {};
  check(!q.owned() && !qc.at("a").pendingEvents &&
            qc.at("a").eventFailures == beforeFailures + 1,
        "production delegate destruction without Run visibly settles queue");
  enqueue(61);
  auto scheduleTicket = q.claimDispatch();
  bool scheduleFailed = false;
  try {
    probe::submitWatchDispatch(scheduleTicket,
      [](auto) { throw std::runtime_error("scheduler unavailable"); }, run, dispose);
  } catch (...) { scheduleFailed = true; }
  check(scheduleFailed && !q.owned() && !q.outstanding() &&
            qc.at("a").eventFailures == beforeFailures + 2,
        "production dispatch exception settles once and permits later dispatch");
  enqueue(62);
  auto ambiguousTicket = q.claimDispatch();
  try {
    probe::submitWatchDispatch(ambiguousTicket,
      [&](auto task) {
        queuedDispatch = std::move(task);
        throw std::runtime_error("submission ownership ambiguous");
      }, run, dispose);
  } catch (...) {}
  queuedDispatch();
  queuedDispatch = {};
  check(!q.owned() && !q.outstanding() &&
            qc.at("a").eventFailures == beforeFailures + 3 &&
            qc.at("a").delivered.back() != 62,
        "late Run after failed submission cannot replay already failed records");
  enqueue(63);
  auto readAheadTicket = q.claimDispatch();
  probe::submitWatchDispatch(readAheadTicket, submit, run, dispose);
  qdrain([] { return true; });
  auto deliveredBeforeRun = qc.at("a").delivered.size();
  check(q.outstanding() == readAheadTicket && !q.claimDispatch(),
        "read-assisted drain leaves submitted delegate ticket outstanding");
  queuedDispatch();
  queuedDispatch = {};
  check(!q.outstanding() && qc.at("a").eventFailures == beforeFailures + 3 &&
            qc.at("a").delivered.size() == deliveredBeforeRun,
        "empty submitted dispatcher disposes normally after read-assisted drain");
  enqueue(70);
  enqueue(71);
  auto budgetStart = ms;
  auto measured = q.drain(1, qfind, [&] { return ms - budgetStart < 5; },
    [&](QueueCapture &, QueueEvent &) { ms += 3; },
    [&](QueueCapture &, QueueEvent &) { ms += 2; }, [&] { return ms; });
  check(measured.records == 1 && measured.budgetStopped && q.owned() == 1 &&
            qc.at("a").maxDrainMs == 5 && qc.at("a").maxDeliveryDelayMs >= 3,
        "source and coalesced visual work share a measured five-ms budget");
  auto stopsBeforeZero = qc.at("a").drainBudgetStops;
  auto noProgress = qdrain([] { return false; });
  check(!noProgress.records && q.owned() == 1 &&
            qc.at("a").drainBudgetStops == stopsBeforeZero + 1,
        "already exhausted request records starvation without consuming a record");
  q.discard(qfind, false);

  probe::PagedRing<int> emptyThenPublished(2000);
  auto emptyPage = emptyThenPublished.read(0, 50, [] { return true; });
  enqueue(72);
  q.drain(1, qfind, [] { return true; },
    [&](QueueCapture &, QueueEvent &) { emptyThenPublished.push(72); },
    [](QueueCapture &, QueueEvent &) {}, [&] { return ms; });
  auto finalWatermark = emptyThenPublished.next - 1;
  check(emptyPage.events.empty() && !emptyPage.nextSequence &&
            finalWatermark == 1 && emptyPage.nextSequence < finalWatermark &&
            !q.owned() && !qc.at("a").pendingEvents &&
            emptyThenPublished.read(0, 50, [] { return true; }).events.size() == 1,
        "page-before-drain can advance final watermark with unchanged cursor and zero pending");
  std::cout << checks << " explicit checks, " << failures << " failures\n";
  return failures ? 1 : 0;
}
