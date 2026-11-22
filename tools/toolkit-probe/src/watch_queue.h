#pragma once
#include "contract.h"
#include <array>
#include <cstdint>
#include <functional>
#include <memory>

namespace probe {
// All methods require the caller's capture lock. This helper never schedules
// work: a bounded drain resumes only on a new event or an explicit watch read.
// Event owns PendingBatch pending and a steady-clock enqueuedMs timestamp.
template <class Event> class WatchQueue {
public:
  static constexpr unsigned capacity = 2000, batchLimit = 50;
  using Ticket = std::uint64_t;
  struct DrainResult {
    unsigned records = 0, failures = 0;
    bool budgetStopped = false;
  };
  std::size_t owned() const noexcept { return owned_; }
  Ticket outstanding() const noexcept { return outstanding_; }

  template <class Lookup> bool enqueue(Event event, Lookup lookup) {
    if (event.pending.recipients.size() > 4)
      throw std::runtime_error("event recipient bound exceeded");
    if (owned_ >= capacity) {
      for (auto &r : event.pending.recipients)
        if (auto *c = lookup(r.id); c && sameRecipient(r, c->id, c->gen))
          ++c->eventQueueDropped;
      return false;
    }
    // Allocate and link before admission: allocation failure owns no counters.
    auto item = std::make_unique<Event>(std::move(event));
    waiting_.push_back(std::move(item));
    markPending(waiting_.back()->pending, lookup);
    ++owned_;
    for (auto &r : waiting_.back()->pending.recipients)
      if (auto *c = lookup(r.id); c && sameRecipient(r, c->id, c->gen))
        c->queueHighWaterMark = (std::max)(
            c->queueHighWaterMark, static_cast<unsigned long long>(owned_));
    return true;
  }
  Ticket claimDispatch() noexcept {
    if (outstanding_ || waiting_.empty())
      return 0;
    return outstanding_ = ++nextTicket_;
  }
  void finishDispatch(Ticket ticket) noexcept {
    if (outstanding_ == ticket)
      outstanding_ = 0;
  }
  template <class Lookup> void discard(Lookup lookup, bool failed) {
    while (!waiting_.empty()) {
      auto item = std::move(waiting_.front());
      waiting_.pop_front();
      if (failed)
        for (auto &r : item->pending.recipients)
          if (auto *c = lookup(r.id); c && sameRecipient(r, c->id, c->gen))
            ++c->eventFailures;
      settle(*item, lookup);
    }
  }
  template <class Lookup> void failDispatch(Ticket ticket, Lookup lookup) {
    // A stale task's disposal cannot cancel a newer dispatcher or its work.
    if (!ticket || outstanding_ != ticket)
      return;
    discard(lookup, true);
    outstanding_ = 0;
  }
  template <class Lookup, class Ready, class Deliver, class Visual, class Now>
  DrainResult drain(unsigned current, Lookup lookup, Ready ready,
                    Deliver deliver, Visual visual, Now now) {
    DrainResult result;
    using CapturePtr = decltype(lookup(std::declval<const std::string &>()));
    // At most four enqueue-time recipients per event. Views/pointers reference
    // retained captures, never an event that is released at the end of a loop.
    std::array<CapturePtr, batchLimit * 4> affected{};
    unsigned affectedCount = 0;
    auto started = now();
    while (result.records < batchLimit && !waiting_.empty()) {
      if (!ready()) {
        result.budgetStopped = true;
        break;
      }
      auto item = std::move(waiting_.front());
      waiting_.pop_front();
      // owned_ deliberately includes item until every recipient settles.
      for (auto &r : item->pending.recipients) {
        auto *c = lookup(r.id);
        if (!c || !sameRecipient(r, c->id, c->gen))
          continue;
        bool first = std::find(affected.begin(), affected.begin() + affectedCount,
                               c) == affected.begin() + affectedCount;
        if (first && affectedCount < affected.size())
          affected[affectedCount++] = c;
        if (r.generation != current)
          continue;
        try {
          deliver(*c, *item);
          c->maxDeliveryDelayMs = (std::max)(c->maxDeliveryDelayMs,
                                             now() - item->enqueuedMs);
          // Each affected capture is sampled at most once in this drain. The
          // adapter checks stop/expiry and the shared budget before dereference.
          if (first)
            visual(*c, *item);
        } catch (...) {
          ++c->eventFailures;
          ++result.failures;
        }
      }
      settle(*item, lookup);
      ++result.records;
    }
    // Observe budget exhaustion even if the final item consumed the remainder.
    result.budgetStopped = !ready() || result.budgetStopped;
    if (!result.records && result.budgetStopped && !waiting_.empty())
      for (auto &r : waiting_.front()->pending.recipients)
        if (auto *c = lookup(r.id); c && affectedCount < affected.size())
          affected[affectedCount++] = c;
    auto elapsed = now() - started;
    for (unsigned i = 0; i < affectedCount; ++i) {
      auto *c = affected[i];
      c->maxDrainMs = (std::max)(c->maxDrainMs, elapsed);
      if (result.budgetStopped)
        ++c->drainBudgetStops;
    }
    return result;
  }

private:
  template <class Lookup> void settle(Event &event, Lookup lookup) {
    if (settlePending(event.pending, lookup))
      --owned_;
  }
  std::deque<std::unique_ptr<Event>> waiting_;
  std::size_t owned_ = 0;
  Ticket outstanding_ = 0, nextTicket_ = 0;
};

// The delegate owns only this ticket, not engine pointers. Both a failed submit
// and destruction of an unrun delegate settle through the same idempotent path.
template <class Submit, class Run, class Dispose>
void submitWatchDispatch(std::uint64_t ticket, Submit submit, Run run,
                         Dispose dispose) {
  struct Lease {
    std::uint64_t ticket;
    Dispose dispose;
    Lease(std::uint64_t t, Dispose d) : ticket(t), dispose(std::move(d)) {}
    ~Lease() {
      try {
        dispose(ticket);
      } catch (...) {
      }
    }
  };
  try {
    auto lease = std::make_shared<Lease>(ticket, dispose);
    submit([lease, run] { run(lease->ticket); });
  } catch (...) {
    dispose(ticket);
    throw;
  }
}
} // namespace probe
