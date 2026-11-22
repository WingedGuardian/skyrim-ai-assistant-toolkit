#include "DevBenchAPI.h"
#include "PCH.h"
#include "contract.h"
#include "logic.h"
#include "watch_queue.h"
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <unordered_map>
using J = nlohmann::json;
using Clock = std::chrono::steady_clock;
namespace {
std::atomic<unsigned> generation{1};
probe::RegistrationGate registrationGate;
// observe() needs a world: the player in a cell, and neither the main menu nor a loading screen open. No load
// message is relied on: quitting to the main menu sends none, and a `coc` from the main menu may send none.
// Recorded from the SKSE messaging callback (game main thread). A handler that ran ON the main thread would
// wait 4 s for a task only that thread can run: refuse instead of freezing the game.
std::atomic<std::thread::id> mainThread{};
struct NotLoaded : std::runtime_error {
  NotLoaded() : std::runtime_error("no game loaded (main menu or loading)") {}
};
const std::string session =
    std::to_string(std::chrono::system_clock::now().time_since_epoch().count());
std::string hex(RE::FormID id) { return std::format("{:08X}", id); }
double stamp() {
  return std::chrono::duration<double>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}
probe::V vec(RE::NiPoint3 p) { return {p.x, p.y, p.z}; }
J xyz(RE::NiPoint3 p) { return J::array({p.x, p.y, p.z}); }
J provenance(RE::TESForm *f) {
  J p = J::array();
  if (f && f->sourceFiles.array)
    for (auto *file : *f->sourceFiles.array)
      if (file)
        p.push_back(file->fileName);
  return p;
}
J identity(RE::TESForm *f) {
  if (!f)
    return nullptr;
  return {
      {"formId", hex(f->GetFormID())},
      {"name", f->GetName() ? J(f->GetName()) : J(nullptr)},
      {"editorId", f->GetFormEditorID() ? J(f->GetFormEditorID()) : J(nullptr)},
      {"formType", std::string(RE::FormTypeToString(f->GetFormType()))},
      {"runtimeSourceFiles", provenance(f)}};
}
using StoredEvent = std::shared_ptr<const J>;
using EventRing = probe::PagedRing<StoredEvent>;
struct Budget {
  Clock::time_point start = Clock::now();
  bool stopped = false;
  unsigned nodes = 0;
  std::function<bool()> invalid;
  std::vector<EventRing::Entry> eventPage;
  std::shared_ptr<const J> visualSnapshot;
  std::vector<std::string_view> eventVisualCaptures;
  bool exhausted() {
    return stopped = stopped || (invalid && invalid()) || ++nodes > 5000 ||
                     Clock::now() - start >= std::chrono::milliseconds(5);
  }
};
void shapes(RE::NiAVObject *root, J &out, Budget &budget, unsigned depth = 0) {
  if (depth > 128) {
    budget.stopped = true;
    return;
  }
  if (!root || budget.exhausted())
    return;
  if (auto *g = root->AsGeometry()) {
    J s = {{"shape", root->name.c_str()},
           {"bounds",
            {{"center", xyz(root->worldBound.center)},
             {"radius", root->worldBound.radius}}},
           {"textures", J::array()}};
    if (auto *prop = g->GetGeometryRuntimeData().shaderProperty.get();
        prop && prop->material) {
      auto *m = prop->material;
      s["shaderType"] = static_cast<unsigned>(m->GetType());
      s["shaderFeature"] = static_cast<int>(m->GetFeature());
      if (m->GetType() == RE::BSShaderMaterial::Type::kLighting) {
        auto *lm = static_cast<RE::BSLightingShaderMaterialBase *>(m);
        if (lm->textureSet)
          for (unsigned i = 0; i < 9; i++) {
            auto *path = lm->textureSet->GetTexturePath(
                static_cast<RE::BSTextureSet::Texture>(i));
            if (path && *path)
              s["textures"].push_back(
                  {{"slot", i},
                   {"path", path},
                   {"source", "runtime lighting texture set"}});
          }
      } else if (m->GetType() == RE::BSShaderMaterial::Type::kEffect) {
        auto *em = static_cast<RE::BSEffectShaderMaterial *>(m);
        s["textures"] = probe::effectTextures(
            em->sourceTexturePath.c_str(), em->greyscaleTexturePath.c_str(),
            em->sourceTexture ? em->sourceTexture->name.c_str() : nullptr,
            em->greyscaleTexture ? em->greyscaleTexture->name.c_str() : nullptr);
      } else
        s["textureCoverage"] = "unavailable: water/base material";
      if (m->GetType() == RE::BSShaderMaterial::Type::kLighting) {
        auto *lm = static_cast<RE::BSLightingShaderMaterialBase *>(m);
        auto add = [&](const std::string &slot, RE::NiSourceTexture *t) {
          if (t)
            s["textures"].push_back({{"slot", slot},
                                     {"path", t->name.c_str()},
                                     {"source", "loaded source texture"}});
        };
        add("rimSoftLighting", lm->rimSoftLightingTexture.get());
        add("specularBackLighting", lm->specularBackLightingTexture.get());
        switch (m->GetFeature()) {
        case RE::BSShaderMaterial::Feature::kEnvironmentMap: {
          auto *x = static_cast<RE::BSLightingShaderMaterialEnvmap *>(m);
          add("environment", x->envTexture.get());
          add("environmentMask", x->envMaskTexture.get());
          break;
        }
        case RE::BSShaderMaterial::Feature::kGlowMap: {
          auto *x = static_cast<RE::BSLightingShaderMaterialGlowmap *>(m);
          add("glow", x->glowTexture.get());
          break;
        }
        case RE::BSShaderMaterial::Feature::kParallax: {
          auto *x = static_cast<RE::BSLightingShaderMaterialParallax *>(m);
          add("height", x->heightTexture.get());
          break;
        }
        case RE::BSShaderMaterial::Feature::kParallaxOcc: {
          auto *x = static_cast<RE::BSLightingShaderMaterialParallaxOcc *>(m);
          add("height", x->heightTexture.get());
          break;
        }
        case RE::BSShaderMaterial::Feature::kFaceGen: {
          auto *x = static_cast<RE::BSLightingShaderMaterialFacegen *>(m);
          add("tint", x->tintTexture.get());
          add("detail", x->detailTexture.get());
          add("subsurface", x->subsurfaceTexture.get());
          break;
        }
        case RE::BSShaderMaterial::Feature::kEye: {
          auto *x = static_cast<RE::BSLightingShaderMaterialEye *>(m);
          add("environment", x->envTexture.get());
          add("environmentMask", x->envMaskTexture.get());
          break;
        }
        case RE::BSShaderMaterial::Feature::kMultilayerParallax: {
          auto *x =
              static_cast<RE::BSLightingShaderMaterialMultiLayerParallax *>(m);
          add("layer", x->layerTexture.get());
          add("environment", x->envTexture.get());
          add("environmentMask", x->envMaskTexture.get());
          break;
        }
        case RE::BSShaderMaterial::Feature::kMultiTexLandLODBlend: {
          auto *x = static_cast<RE::BSLightingShaderMaterialLandscape *>(m);
          for (unsigned i = 0; i < 5; i++) {
            add("landscapeDiffuse" + std::to_string(i),
                x->landscapeDiffuseTexture[i].get());
            add("landscapeNormal" + std::to_string(i),
                x->landscapeNormalTexture[i].get());
          }
          add("terrainOverlay", x->terrainOverlayTexture.get());
          add("terrainNoise", x->terrainNoiseTexture.get());
          break;
        }
        case RE::BSShaderMaterial::Feature::kLODLand: {
          auto *x = static_cast<RE::BSLightingShaderMaterialLODLandscape *>(m);
          add("parentDiffuse", x->parentDiffuseTexture.get());
          add("parentNormal", x->parentNormalTexture.get());
          add("landscapeNoise", x->landscapeNoiseTexture.get());
          break;
        }
        default:
          break;
        }
        if (lm->diffuseTexture)
          s["textures"].push_back({{"slot", "diffuseLoaded"},
                                   {"path", lm->diffuseTexture->name.c_str()},
                                   {"source", "loaded source texture"}});
        if (lm->normalTexture)
          s["textures"].push_back({{"slot", "normalLoaded"},
                                   {"path", lm->normalTexture->name.c_str()},
                                   {"source", "loaded source texture"}});
      }
    }
    out.push_back(std::move(s));
  }
  if (auto *node = root->AsNode())
    for (auto &c : node->GetChildren()) {
      if (budget.exhausted())
        break;
      shapes(c.get(), out, budget, depth + 1);
    }
}
J reference(RE::TESObjectREFR *ref, Budget &budget, bool detail) {
  auto j = identity(ref);
  j["base"] = identity(ref->GetBaseObject());
  if (auto *model = skyrim_cast<RE::TESModel *>(ref->GetBaseObject()))
    j["mesh"] = model->GetModel();
  j["baseFormId"] = ref->GetBaseObject()
                        ? J(hex(ref->GetBaseObject()->GetFormID()))
                        : J(nullptr);
  j["position"] = xyz(ref->GetPosition());
  j["loaded3D"] = ref->Get3D() != nullptr;
  j["disabled"] = ref->IsDisabled();
  j["cell"] = identity(ref->GetParentCell());
  if (auto *root = ref->Get3D()) {
    j["bounds"] = {{"center", xyz(root->worldBound.center)},
                   {"radius", root->worldBound.radius}};
    if (detail) {
      j["shapes"] = J::array();
      shapes(root, j["shapes"], budget);
    }
  }
  return j;
}
RE::FormID form(const J &a, const char *key, RE::FormID def = 0x14) {
  if (!a.contains(key))
    return def;
  auto s = a.at(key).get<std::string>();
  if (s == "player")
    return 0x14;
  if (s.starts_with("0x") || s.starts_with("0X"))
    s = s.substr(2);
  if (!probe::validForm(s))
    throw std::runtime_error("invalid form ID");
  return static_cast<RE::FormID>(std::stoul(s, nullptr, 16));
}
J effects(RE::Actor *actor, Budget &budget, J &coverage) {
  J result = J::array();
  auto *target = actor->AsMagicTarget();
  if (!target) {
    coverage = {{"status", "unavailable"},
                {"reason", "MagicTarget unavailable"}};
    return result;
  }
  coverage = {{"status", "checked"}};
  auto append = [&](RE::ActiveEffect *e) {
    if (budget.exhausted())
      return RE::BSContainer::ForEachResult::kStop;
    if (e) {
      auto j = identity(e->GetBaseObject());
      j["source"] = identity(e->spell);
      j["elapsed"] = e->elapsedSeconds;
      j["duration"] = e->duration;
      auto caster = e->GetCasterActor();
      j["caster"] = identity(caster.get());
      result.push_back(std::move(j));
    }
    return RE::BSContainer::ForEachResult::kContinue;
  };
  // MagicTarget::VisitEffects is the engine's own iteration (RELOCATION_ID 33756/34540) on SE, AE and VR;
  // walking GetActiveEffectList() directly read a list the game may be changing.
  struct Visitor : RE::MagicTarget::ForEachActiveEffectVisitor {
    decltype(append) &fn;
    explicit Visitor(decltype(append) &f) : fn(f) {}
    RE::BSContainer::ForEachResult Accept(RE::ActiveEffect *e) override {
      return fn(e);
    }
  } visitor{append};
  target->VisitEffects(visitor);
  coverage["status"] = budget.stopped ? "incomplete" : "checked";
  return result;
}
struct VisualObservation {
  J values = J::array();
  bool available = true;
  bool complete = false;
};
VisualObservation visuals(RE::Actor *actor, Budget &b) {
  J out = J::array();
  auto *lists = RE::ProcessLists::GetSingleton();
  if (!lists)
    return {J::array(), false, false};
  auto handle = actor->GetHandle();
  lists->ForEachMagicTempEffect([&](RE::BSTempEffect *e) {
    if (b.exhausted())
      return RE::BSContainer::ForEachResult::kStop;
    auto *ref = e->As<RE::ReferenceEffect>();
    if (!ref || ref->target != handle || ref->finished)
      return RE::BSContainer::ForEachResult::kContinue;
    if (out.size() >= 100) {
      b.stopped = true;
      return RE::BSContainer::ForEachResult::kStop;
    }
    J j = {{"effectId", e->effectID},
           {"age", e->age},
           {"attribution", "unknown"},
           {"textures", J::array()}};
    if (auto *model = e->As<RE::ModelReferenceEffect>()) {
      j["kind"] = "artObject";
      j["form"] = identity(model->artObject);
      j["shapes"] = J::array();
      shapes(model->artObject3D.get(), j["shapes"], b);
    }
    if (auto *shader = e->As<RE::ShaderReferenceEffect>()) {
      j["kind"] = "effectShader";
      j["form"] = identity(shader->effectData);
      auto add = [&](const char *slot, RE::NiSourceTexture *t) {
        if (t)
          j["textures"].push_back({{"slot", slot},
                                   {"path", t->name.c_str()},
                                   {"source", "loaded source texture"}});
      };
      add("fill", shader->fillTexture.get());
      add("holes", shader->holesTexture.get());
      add("palette", shader->paletteTexture.get());
      add("particle", shader->particleShaderTexture.get());
      add("particlePalette", shader->particlePaletteTexture.get());
    }
    out.push_back(std::move(j));
    return RE::BSContainer::ForEachResult::kContinue;
  });
  return {std::move(out), true, !b.stopped};
}
struct Capture {
  unsigned gen;
  RE::FormID actor;
  std::string id;
  Clock::time_point expiry;
  EventRing ring{2000};
  bool stopped = false;
  std::shared_ptr<const J> visualSnapshot =
      std::make_shared<const J>(J::array());
  // The address is only a cheap prefilter. A later actor can reuse the memory (and an FF form ID), so a
  // match is confirmed against the handle taken at start, whose age bits differ for a recycled slot.
  std::uintptr_t targetAddress = 0;
  RE::ObjectRefHandle target;
  // Captures this start superseded; restored if the start reply can never be delivered.
  std::vector<std::string> superseded;
  std::string terminalReason = "stopped";
  bool visualSnapshotComplete = false;
  std::string visualStatus = "not checked";
  unsigned long long pendingEvents = 0;
  unsigned long long eventQueueDropped = 0, eventFailures = 0,
                     visualEventsDropped = 0;
  double maxDeliveryDelayMs = 0, maxDrainMs = 0;
  unsigned long long queueHighWaterMark = 0, drainBudgetStops = 0;
  probe::VisualBaseline baseline;
};
void addEvent(Capture &c, J event) {
  c.ring.push(std::make_shared<const J>(std::move(event)));
}
void updateVisuals(Capture &c, VisualObservation observation, Budget &budget) {
  c.visualSnapshotComplete = observation.available && observation.complete;
  c.visualStatus = !observation.available ? "unavailable"
                   : observation.complete ? "checked"
                                          : "incomplete";
  if (!c.visualSnapshotComplete)
    return;
  std::vector<std::string> ids;
  std::unordered_map<std::string, const J *> current, prior;
  for (auto &v : observation.values) {
    if (budget.exhausted()) {
      c.visualSnapshotComplete = false;
      c.visualStatus = "incomplete";
      return;
    }
    auto id = std::to_string(v["effectId"].get<unsigned>());
    ids.push_back(id);
    current.emplace(id, &v);
  }
  for (auto &v : *c.visualSnapshot) {
    if (budget.exhausted()) {
      c.visualSnapshotComplete = false;
      c.visualStatus = "incomplete";
      return;
    }
    prior.emplace(std::to_string(v["effectId"].get<unsigned>()), &v);
  }
  auto delta = c.baseline.observe(ids, true);
  auto event = [&](const char *kind, const J &visual) {
    if (budget.exhausted()) {
      ++c.visualEventsDropped;
      return;
    }
    J detail = {{"effectId", visual["effectId"]},
                {"kind", visual.value("kind", "unknown")},
                {"attribution", "unknown"}};
    if (visual.contains("form"))
      detail["form"] = visual["form"];
    addEvent(c, J{{"kind", kind},
                  {"time", stamp()},
                  {"visual", std::move(detail)},
                  {"attribution", "unknown"}});
  };
  for (auto &id : delta.added)
    event("visualAttached", *current.at(id));
  for (auto &id : delta.removed)
    event("visualRemoved", *prior.at(id));
  c.visualSnapshot = std::make_shared<const J>(std::move(observation.values));
}
std::mutex captureMutex;
std::map<std::string, Capture> captures;
unsigned captureCounter = 0;

Capture *findCapture(const std::string &id) noexcept {
  auto it = captures.find(id);
  return it == captures.end() ? nullptr : &it->second;
}
double monotonicMs() {
  return std::chrono::duration<double, std::milli>(
             Clock::now().time_since_epoch()).count();
}
struct PendingEvent {
  probe::PendingBatch pending;
  RE::NiPointer<RE::TESObjectREFR> target;
  RE::FormID source;
  const char *kind;
  double time, enqueuedMs;
};
probe::WatchQueue<PendingEvent> eventQueue;
std::atomic<unsigned long long> eventBoundaryFailures{0};
// Caller owns captureMutex. No submission occurs from a drain.
void drainEvents(Budget &budget) {
  auto result = eventQueue.drain(
      generation.load(), findCapture, [&] { return !budget.exhausted(); },
      [&](Capture &c, PendingEvent &event) {
        auto *sourceForm = RE::TESForm::LookupByID(event.source);
        addEvent(c, J{{"kind", event.kind},
                      {"time", event.time},
                      {"sourceFormId", hex(event.source)},
                      {"source", identity(sourceForm)},
                      {"sourceKind",
                       std::string_view(event.kind) == "spellCastByActor" ? "spell"
                       : std::string_view(event.kind) == "magicEffectApplied"
                           ? "magicEffect" : "hitSource"},
                      {"sourceStatus", sourceForm ? "checked" : "unavailable"},
                      {"attribution", "direct"}});
      },
      [&](Capture &c, PendingEvent &event) {
        budget.eventVisualCaptures.push_back(c.id);
        if (c.stopped || Clock::now() >= c.expiry)
          return;
        if (budget.exhausted()) {
          c.visualStatus = "incomplete";
          c.visualSnapshotComplete = false;
          return;
        }
        auto *actor = event.target->As<RE::Actor>();
        if (actor)
          updateVisuals(c, visuals(actor, budget), budget);
        else {
          c.visualStatus = "unavailable";
          c.visualSnapshotComplete = false;
        }
      }, monotonicMs);
  eventBoundaryFailures += result.failures;
}
void failEventDispatch(std::uint64_t ticket) noexcept {
  try {
    std::lock_guard lock(captureMutex);
    eventQueue.failDispatch(ticket, findCapture);
  } catch (...) {
    ++eventBoundaryFailures;
  }
}
void runEventDispatch(std::uint64_t ticket) noexcept {
  try {
    Budget budget;
    std::lock_guard lock(captureMutex);
    if (eventQueue.outstanding() != ticket)
      return;
    try {
      drainEvents(budget);
    } catch (...) {
      ++eventBoundaryFailures;
      eventQueue.discard(findCapture, true);
    }
    // Never schedule a successor: a later event/read is the continuation.
    eventQueue.finishDispatch(ticket);
  } catch (...) {
    ++eventBoundaryFailures;
    failEventDispatch(ticket);
  }
}
J watch(const J &args, Budget &budget) {
  std::lock_guard lock(captureMutex);
  auto now = Clock::now();
  if (args.value("read", false) || args.value("stop", false)) {
    auto id = args.at("captureId").get<std::string>();
    auto it = captures.find(id);
    if (it == captures.end())
      throw std::runtime_error("unknown captureId");
    auto &c = it->second;
    auto after = probe::integer(args, "after", 0, 0, 9007199254740991ULL);
    if (after >= c.ring.next)
      throw std::runtime_error("cursor ahead of capture sequence");
    // Stop becomes terminal before any accepted-event drain or visual work.
    if (args.value("stop", false)) {
      c.stopped = true;
      c.terminalReason = "stopped";
    }
    // Page shallow-owned events before pending delivery can evict ring rows.
    // Expansion/serialization is listener-only.
    auto page = c.ring.read(after, 50, [&] { return !budget.exhausted(); });
    budget.eventPage = std::move(page.events);
    drainEvents(budget);
    if (c.gen == generation && !c.stopped && Clock::now() < c.expiry &&
        std::find(budget.eventVisualCaptures.begin(),
                  budget.eventVisualCaptures.end(), c.id) ==
            budget.eventVisualCaptures.end() && !budget.exhausted()) {
      auto ref = c.target.get();
      auto *actor = ref ? ref->As<RE::Actor>() : nullptr;
      if (actor && actor->GetFormID() == c.actor) {
        auto snapshot = visuals(actor, budget);
        updateVisuals(c, std::move(snapshot), budget);
      } else {
        c.visualStatus = "unavailable";
        c.visualSnapshotComplete = false;
      }
    }
    budget.visualSnapshot = c.visualSnapshot;
    auto status = probe::captureStatus(
        c.gen, generation.load(), c.stopped, c.terminalReason,
        std::chrono::duration<double>(now.time_since_epoch()).count(),
        std::chrono::duration<double>(c.expiry.time_since_epoch()).count());
    return {{"captureId", id},
            {"actor", hex(c.actor)},
            {"events", J::array()},
            {"nextSequence", page.nextSequence},
            {"latestSequence", c.ring.next - 1},
            {"hasMore", page.nextSequence < c.ring.next - 1},
            {"dropped", c.ring.next - 1 - c.ring.events.size()},
            {"cursorLost", page.lost},
            {"active", status == "active"},
            {"status", status},
            {"visualSnapshot", nullptr},
            {"visualSnapshotComplete", c.visualSnapshotComplete},
            {"visualStatus", c.visualStatus},
            {"pendingEvents", c.pendingEvents},
            {"eventQueueDropped", c.eventQueueDropped},
            {"eventFailures", c.eventFailures},
            {"visualEventsDropped", c.visualEventsDropped},
            {"maxDeliveryDelayMs", c.maxDeliveryDelayMs},
            {"queueHighWaterMark", c.queueHighWaterMark},
            {"maxDrainMs", c.maxDrainMs},
            {"drainBudgetStops", c.drainBudgetStops},
            {"visualSampling", "per-actor-capture-drain"}};
  }
  auto actor = form(args, "actor");
  auto *actorRef = RE::TESForm::LookupByID<RE::Actor>(actor);
  if (!actorRef)
    throw std::runtime_error("actor unavailable");
  auto secs = probe::integer(args, "seconds", 180, 1, 600);
  unsigned active = 0;
  for (auto &[_, c] : captures)
    if (c.gen == generation && !c.stopped && now < c.expiry) {
      if (c.actor != actor)
        ++active;
    }
  if (active >= 4)
    throw std::runtime_error("four active actors limit");
  if (captures.size() >= 32) {
    auto it =
        std::find_if(captures.begin(), captures.end(), [&](const auto &item) {
          auto &c = item.second;
          return c.pendingEvents == 0 &&
                 (c.stopped || c.gen != generation || now >= c.expiry);
        });
    if (it != captures.end())
      captures.erase(it);
    else
      throw std::runtime_error("capture retention limit");
  }
  auto id = session + "-" + std::to_string(++captureCounter);
  auto [created, _] =
      captures.emplace(id, Capture{generation.load(), actor, id,
                                   now + std::chrono::seconds(secs)});
  auto &c = created->second;
  c.targetAddress = reinterpret_cast<std::uintptr_t>(
      static_cast<RE::TESObjectREFR *>(actorRef));
  c.target = actorRef->GetHandle();
  for (auto &[otherId, old] : captures)
    if (otherId != id && old.actor == actor && old.gen == generation &&
        !old.stopped && now < old.expiry) {
      old.stopped = true;
      old.terminalReason = "superseded";
      c.superseded.push_back(otherId);
    }
  auto snapshot = visuals(actorRef, budget);
  updateVisuals(c, std::move(snapshot), budget);
  budget.visualSnapshot = c.visualSnapshot;
  return {{"captureId", id},
          {"actor", hex(actor)},
          {"events", J::array()},
          {"nextSequence", 0},
          {"latestSequence", c.ring.next - 1},
          {"hasMore", false},
          {"dropped", 0},
          {"cursorLost", false},
          {"active", true},
          {"status", "active"},
          {"visualSnapshot", nullptr},
          {"visualSnapshotComplete", c.visualSnapshotComplete},
          {"visualStatus", c.visualStatus},
          {"pendingEvents", c.pendingEvents},
          {"eventQueueDropped", 0},
          {"eventFailures", 0},
          {"visualEventsDropped", 0},
          {"maxDeliveryDelayMs", 0},
          {"queueHighWaterMark", 0},
          {"maxDrainMs", 0},
          {"drainBudgetStops", 0},
          {"visualSampling", "per-actor-capture-drain"}};
}
struct EventSinks : RE::BSTEventSink<RE::TESMagicEffectApplyEvent>,
                    RE::BSTEventSink<RE::TESSpellCastEvent>,
                    RE::BSTEventSink<RE::TESHitEvent> {
  void record(RE::NiPointer<RE::TESObjectREFR> target, RE::FormID source,
              const char *kind) noexcept {
    // Timestamps belong to the callback, never to eventual publication.
    const auto time = stamp(), enqueuedMs = monotonicMs();
    std::uint64_t ticket = 0;
    bool admitted = false;
    std::vector<probe::RecipientBinding> recipients;
    try {
      if (!target)
        return;
      auto now = Clock::now();
      {
        std::lock_guard lock(captureMutex);
        const auto address = reinterpret_cast<std::uintptr_t>(target.get());
        std::optional<RE::ObjectRefHandle> handle;   // taken only for an address match: rare
        for (auto &[id, c] : captures)
          if (c.targetAddress == address && c.gen == generation &&
              !c.stopped && now < c.expiry) {
            if (!handle)
              handle = target->GetHandle();
            if (*handle == c.target && target->GetFormID() == c.actor)
              recipients.push_back({id, c.gen});
          }
        if (recipients.empty())
          return;
        PendingEvent event{{recipients}, std::move(target), source, kind,
                           time, enqueuedMs};
        admitted = eventQueue.enqueue(std::move(event), findCapture);
        // Every pending capture experiences the global occupancy, irrespective
        // of which actor supplied this callback. Never dereference game state.
        for (auto &[_, c] : captures)
          if (c.pendingEvents)
            c.queueHighWaterMark = (std::max)(
                c.queueHighWaterMark,
                static_cast<unsigned long long>(eventQueue.owned()));
        // Even a rejected overflow callback can wake a quiet backlog.
        ticket = eventQueue.claimDispatch();
      }
      if (!ticket)
        return;
      auto *tasks = SKSE::GetTaskInterface();
      if (!tasks)
        throw std::runtime_error("event task dispatcher unavailable");
      // SKSE holds its task-queue lock during Run; never submit while holding
      // captureMutex. Delegate disposal owns only a numeric dispatcher ticket.
      probe::submitWatchDispatch(ticket,
          [tasks](auto run) { tasks->AddTask(std::move(run)); },
          runEventDispatch, failEventDispatch);
    } catch (...) {
      ++eventBoundaryFailures;
      if (ticket)
        failEventDispatch(ticket);
      else if (!admitted) {
        try {
          std::lock_guard lock(captureMutex);
          for (auto &r : recipients)
            if (auto *c = findCapture(r.id);
                c && probe::sameRecipient(r, c->id, c->gen))
              ++c->eventFailures;
        } catch (...) {
        }
      }
    }
  }
  RE::BSEventNotifyControl
  ProcessEvent(const RE::TESMagicEffectApplyEvent *e,
               RE::BSTEventSource<RE::TESMagicEffectApplyEvent> *) override {
    try {
      if (e)
        record(e->target, e->magicEffect, "magicEffectApplied");
    } catch (...) {
      ++eventBoundaryFailures;
    }
    return RE::BSEventNotifyControl::kContinue;
  }
  RE::BSEventNotifyControl
  ProcessEvent(const RE::TESSpellCastEvent *e,
               RE::BSTEventSource<RE::TESSpellCastEvent> *) override {
    try {
      if (e)
        record(e->object, e->spell, "spellCastByActor");
    } catch (...) {
      ++eventBoundaryFailures;
    }
    return RE::BSEventNotifyControl::kContinue;
  }
  RE::BSEventNotifyControl
  ProcessEvent(const RE::TESHitEvent *e,
               RE::BSTEventSource<RE::TESHitEvent> *) override {
    try {
      if (e)
        record(e->target, e->source, "hit");
    } catch (...) {
      ++eventBoundaryFailures;
    }
    return RE::BSEventNotifyControl::kContinue;
  }
} sinks;
J observe(const std::string &op, const J &a, Budget &b) {
  auto *player = RE::PlayerCharacter::GetSingleton();
  if (!player)
    throw std::runtime_error("player unavailable");
  auto *ui = RE::UI::GetSingleton();
  if (!player->GetParentCell() ||
      (ui && (ui->IsMenuOpen(RE::MainMenu::MENU_NAME) ||
              ui->IsMenuOpen(RE::LoadingMenu::MENU_NAME))))
    throw NotLoaded();
  if (op == "watch")
    return watch(a, b);
  if (op == "ref") {
    auto *ref =
        RE::TESForm::LookupByID<RE::TESObjectREFR>(form(a, "formId", 0));
    if (!ref)
      throw std::runtime_error("reference unavailable");
    return {{"ref", reference(ref, b, true)}};
  }
  if (op == "effects") {
    auto *actor = RE::TESForm::LookupByID<RE::Actor>(form(a, "actor"));
    if (!actor)
      throw std::runtime_error("actor unavailable");
    J coverage;
    auto magic = effects(actor, b, coverage);
    auto visual = visuals(actor, b);
    return {{"effects", std::move(magic)},
            {"effectsCoverage", coverage},
            {"visuals", std::move(visual.values)},
            {"visualsCoverage",
             {{"status", !visual.available ? "unavailable"
                         : visual.complete ? "checked"
                                           : "incomplete"}}}};
  }
  double range =
      a.value(op == "look" ? "max" : "radius", op == "look" ? 2000. : 500.);
  int limit = a.value("limit", 20);
  if (!std::isfinite(range) || range <= 0 || range > 10000 || limit < 1 ||
      limit > 100)
    throw std::runtime_error("range/limit outside caps");
  RE::NiPoint3 origin = player->GetPosition(), direction{0, 1, 0};
  J ray = nullptr;
  if (op == "look") {
    auto from = a.value("from", REL::Module::IsVR() ? "hmd" : "crosshair");
    if (!REL::Module::IsVR() || from == "crosshair") {
      auto *camera = RE::PlayerCamera::GetSingleton();
      if (!camera || !camera->cameraRoot)
        throw std::runtime_error("camera unavailable");
      origin = camera->cameraRoot->world.translate;
      direction = camera->cameraRoot->world.rotate * direction;
      ray = {{"origin", xyz(origin)},
             {"direction", xyz(direction)},
             {"from", "camera center"}};
    } else {
      auto *vr = player->GetVRNodeData();
      if (!vr)
        throw std::runtime_error("VR nodes unavailable");
      RE::NiAVObject *node = from == "hmd"     ? vr->HmdNode.get()
                             : from == "right" ? vr->RightWandNode.get()
                             : from == "left"  ? vr->LeftWandNode.get()
                                               : nullptr;
      if (!node)
        throw std::runtime_error("pose unavailable");
      origin = node->world.translate;
      direction = node->world.rotate * direction;
      ray = {{"origin", xyz(origin)},
             {"direction", xyz(direction)},
             {"from", from}};
    }
    if (direction.Unitize() <= 0)
      throw std::runtime_error("invalid pose direction");
  } else if (a.contains("at") && a["at"].is_array()) {
    auto v = a.at("at").get<std::vector<float>>();
    if (v.size() != 3)
      throw std::runtime_error("at needs xyz");
    origin = {v[0], v[1], v[2]};
  }
  J collision = {{"status", "not checked"}};
  RE::TESObjectREFR *hit = nullptr;
  J refs = J::array(), no3D = J::array();
  bool no3DLimited = false;
  auto *tes = RE::TES::GetSingleton();
  if (!tes)
    throw std::runtime_error("world unavailable");
  if (op == "look" && !b.exhausted()) {
    RE::bhkPickData pick{};
    auto scale = RE::bhkWorld::GetWorldScale();
    auto end = origin + direction * static_cast<float>(range);
    pick.rayInput.from = RE::hkVector4(origin * scale);
    pick.rayInput.to = RE::hkVector4(end * scale);
    pick.rayOutput.Reset();
    auto *object = tes->Pick(pick);
    if (pick.rayOutput.rootCollidable)
      hit = RE::TESHavokUtilities::FindCollidableRef(
          *pick.rayOutput.rootCollidable);
    if (!hit && object)
      hit = object->GetUserData();
    bool self = hit == player;
    if (self)
      hit = nullptr;
    bool hitSomething = pick.rayOutput.HasHit();
    collision = {{"status", pick.pickFailed ? "failed"
                            : self          ? "blocked by the player's own collision"
                                            : "checked"},
                 {"method", "TES::Pick Havok ray"},
                 {"hit", hit ? reference(hit, b, true) : J(nullptr)},
                 {"distance", hit && hitSomething
                                  ? J(pick.rayOutput.hitFraction * range)
                                  : J(nullptr)}};
  }
  tes->ForEachReference([&](RE::TESObjectREFR *ref) {
    if (b.exhausted())
      return RE::BSContainer::ForEachResult::kStop;
    if (!ref || ref == player)
      return RE::BSContainer::ForEachResult::kContinue;
    if (a.contains("types")) {
      auto type = ref->GetBaseObject()
                      ? std::string(RE::FormTypeToString(
                            ref->GetBaseObject()->GetFormType()))
                      : std::string{};
      auto filter = a.at("types").get<std::string>();
      bool found = false;
      size_t offset = 0;
      while (offset <= filter.size()) {
        auto end = filter.find(',', offset);
        auto part = filter.substr(offset, end - offset);
        if (part == type)
          found = true;
        if (end == std::string::npos)
          break;
        offset = end + 1;
      }
      if (!found)
        return RE::BSContainer::ForEachResult::kContinue;
    }
    auto *root = ref->Get3D();
    if (!root) {
      if (no3D.size() < static_cast<unsigned>(limit))
        no3D.push_back(reference(ref, b, false));
      else
        no3DLimited = true;
      return RE::BSContainer::ForEachResult::kContinue;
    }
    double d =
        op == "look"
            ? probe::raySphere(vec(origin), vec(direction),
                               vec(root->worldBound.center),
                               root->worldBound.radius, range)
            : probe::sphereDistance(vec(origin), vec(root->worldBound.center),
                                    root->worldBound.radius);
    if (d < 0 || d > range)
      return RE::BSContainer::ForEachResult::kContinue;
    auto j = reference(ref, b, true);
    if (!j["shapes"].empty()) {
      std::vector<double> distances;
      std::vector<std::string> names;
      for (auto &shape : j["shapes"]) {
        auto center = shape["bounds"]["center"].get<std::vector<double>>();
        probe::V c{center[0], center[1], center[2]};
        auto sd = op == "look"
                      ? probe::raySphere(vec(origin), vec(direction), c,
                                         shape["bounds"]["radius"], range)
                      : probe::sphereDistance(vec(origin), c,
                                              shape["bounds"]["radius"]);
        distances.push_back(sd);
        names.push_back(shape["shape"]);
      }
      d = probe::chooseDistance(distances);
      if (d < 0 || d > range)
        return RE::BSContainer::ForEachResult::kContinue;
      for (size_t i = 0; i < distances.size(); ++i)
        if (distances[i] == d) {
          j["shape"] = names[i];
          break;
        }
      j["boundMethod"] = "loaded shape world sphere";
    } else
      j["boundMethod"] = "loaded root world sphere (no shape observed)";
    j["distance"] = d;
    refs.push_back(std::move(j));
    return RE::BSContainer::ForEachResult::kContinue;
  });
  std::sort(refs.begin(), refs.end(), [](const J &l, const J &r) {
    return l["distance"].get<double>() < r["distance"].get<double>();
  });
  if (hit) {
    auto hitId = hex(hit->GetFormID());
    for (auto it = refs.begin(); it != refs.end();)
      if ((*it)["formId"] == hitId)
        it = refs.erase(it);
      else
        ++it;
    auto j = reference(hit, b, true);
    j["distance"] = collision["distance"];
    j["boundMethod"] = "collision";
    refs.insert(refs.begin(), std::move(j));
  }
  bool limited = refs.size() > static_cast<unsigned>(limit);
  while (refs.size() > static_cast<unsigned>(limit))
    refs.erase(refs.end() - 1);
  return {{"refs", refs},
          {"no3D", no3D},
          {"no3DTruncated", no3DLimited},
          {"ray", ray},
          {"collision", collision},
          {"resultLimitReached", limited},
          {"rankingComplete", !b.stopped}};
}
struct Request {
  std::mutex mutex;
  std::condition_variable ready;
  bool done = false;
  std::atomic<bool> cancelled = false;
  unsigned gen;
  Clock::time_point deadline;
  J response;
  std::vector<EventRing::Entry> eventPage;
  std::shared_ptr<const J> visualSnapshot;
  bool failed = false;
};
bool alive(const std::shared_ptr<Request> &r) {
  return !r->cancelled &&
         probe::mayRun(
             r->gen, generation.load(),
             std::chrono::duration<double>(Clock::now().time_since_epoch())
                 .count(),
             std::chrono::duration<double>(r->deadline.time_since_epoch())
                 .count());
}
J envelope() {
  return {{"schemaVersion", 1},
          {"ok", true},
          {"source", "ToolkitProbe"},
          {"runtime", REL::Module::IsVR()   ? "VR"
                      : REL::Module::IsAE() ? "AE"
                                            : "SE"},
          {"sessionId", session},
          {"sessionGeneration", generation.load()},
          {"generation", generation.load()},
          {"observedAt", stamp()},
          {"eventBoundaryFailures", eventBoundaryFailures.load()},
          {"coverage",
           {{"collision",
             "TES::Pick source contract; live VR qualification pending"},
            {"bounds", "loaded root spheres; no origin prefilter"},
            {"textures", "engine assigned lighting/effect material paths and "
                         "loaded names; not GPU bindings"},
            {"visuals", "ProcessLists model/shader reference effects; direct "
                        "form, unknown initiating source"},
            {"watch",
             "apply/hit/actor-origin cast events; main-thread visual snapshots "
             "at start/read/coalesced actor-capture drain; brief transitions "
             "may be missed; backlog resumes on event/read, never self-schedules"},
            {"runtimeQualification", "pending live tests"}}},
          {"truncated", false},
          {"timings", {{"budgetMs", 5}}}};
}
void handler(void *ctx, const char *raw, void *sink,
             DevBenchAPI::WriteFn write) noexcept {
  probe::emit(
      [&]() -> std::string {
        J response;
        try {
          response = envelope();
          if (raw && strnlen(raw, 16385) > 16384)
            throw std::runtime_error("arguments exceed 16 KiB");
          auto op = std::string(static_cast<const char *>(ctx));
          auto args = probe::validate(J::parse(raw ? raw : "{}"), op,
                                      REL::Module::IsVR());
          if (std::this_thread::get_id() == mainThread.load())
            throw std::runtime_error(
                "called on the game's main thread; it would wait on itself");
          auto req = std::make_shared<Request>();
          req->gen = generation;
          req->deadline = Clock::now() + std::chrono::seconds(4);
          auto *tasks = SKSE::GetTaskInterface();
          if (!tasks)
            throw std::runtime_error("task dispatcher unavailable");
          tasks->AddTask([req, args = std::move(args), op] {
            try {
              if (!alive(req))
                return;
              J out = envelope();
              Budget budget;
              budget.invalid = [req] { return !alive(req); };
              try {
                out["data"] = observe(op, args, budget);
                out["truncated"] =
                    budget.stopped ||
                    (out["data"].contains("resultLimitReached") &&
                     out["data"]["resultLimitReached"].get<bool>()) ||
                    (out["data"].contains("no3DTruncated") &&
                     out["data"]["no3DTruncated"].get<bool>());
                out["timings"]["mainThreadMs"] =
                    std::chrono::duration<double, std::milli>(Clock::now() -
                                                              budget.start)
                        .count();
              } catch (const NotLoaded &e) {
                out["ok"] = false;
                out["error"] = {{"code", "not_loaded"}, {"message", e.what()}};
              } catch (const std::exception &e) {
                out["ok"] = false;
                out["error"] = {{"code", "observation_failed"},
                                {"message", e.what()}};
              } catch (...) {
                out["ok"] = false;
                out["error"] = {{"code", "native_exception"},
                                {"message", "unknown native C++ exception"}};
              }
              bool delivered = false;
              std::string started;
              if (op == "watch" && out.contains("data") &&
                  !args.value("read", false) && !args.value("stop", false))
                started = out["data"].value("captureId", std::string{});
              {
                std::lock_guard lock(req->mutex);
                if (alive(req)) {
                  req->response = std::move(out);
                  req->eventPage = std::move(budget.eventPage);
                  req->visualSnapshot = std::move(budget.visualSnapshot);
                  req->done = true;
                  req->ready.notify_one();
                  delivered = true;
                }
              }
              // Nobody will ever learn this capture's id: stop it rather than hold an actor slot until it
              // expires, and give back the captures it superseded. Never under req->mutex.
              if (!delivered && !started.empty()) {
                std::lock_guard lock(captureMutex);
                if (auto *c = findCapture(started)) {
                  c->stopped = true;
                  c->terminalReason = "abandoned: start reply undeliverable";
                  for (auto &id : c->superseded)
                    if (auto *old = findCapture(id);
                        old && old->terminalReason == "superseded" &&
                        old->gen == generation && Clock::now() < old->expiry) {
                      old->stopped = false;
                      old->terminalReason = "stopped";
                    }
                }
              }
            } catch (...) {
              try {
                std::lock_guard lock(req->mutex);
                req->failed = true;
                req->done = true;
                req->ready.notify_one();
              } catch (...) {
              }
            }
          });
          std::unique_lock lock(req->mutex);
          if (!req->ready.wait_until(lock, req->deadline,
                                     [&] { return req->done; })) {
            req->cancelled = true;
            throw std::runtime_error(
                "main thread busy, deadline expired or session invalidated");
          }
          if (req->failed)
            throw std::runtime_error("native observation boundary failed");
          response = std::move(req->response);
          // Only owned JSON crosses to the listener. Event expansion and
          // UTF8-safe encoding do not occupy the game thread.
          if (response.value("ok", false) && response.contains("data")) {
            if (req->visualSnapshot)
              response["data"]["visualSnapshot"] = *req->visualSnapshot;
            for (auto &entry : req->eventPage) {
              auto event = *entry.value;
              event["sequence"] = entry.sequence;
              response["data"]["events"].push_back(std::move(event));
            }
          }
        } catch (const std::exception &e) {
          if (!response.is_object())
            response = J::parse(probe::fallbackResponse);
          response["schemaVersion"] = 1;
          response["source"] = "ToolkitProbe";
          response["ok"] = false;
          response["error"] = {{"code", "request_failed"},
                               {"message", e.what()}};
        } catch (...) {
          if (!response.is_object())
            response = J::parse(probe::fallbackResponse);
          response["schemaVersion"] = 1;
          response["source"] = "ToolkitProbe";
          response["ok"] = false;
          response["error"] = {{"code", "native_exception"},
                               {"message", "unknown native C++ exception"}};
        }
        response["encoding"] = "utf8-replacement";
        return probe::safeDump(response);
      },
      [&](const char *result) { write(sink, result); });
}
void message(SKSE::MessagingInterface::Message *m) {
  if (!m)
    return;
  mainThread = std::this_thread::get_id();
  if (m->type == SKSE::MessagingInterface::kPreLoadGame ||
      m->type == SKSE::MessagingInterface::kNewGame) {
    std::lock_guard lock(captureMutex);
    ++generation;
    // Release owned targets and settle stale admissions even if no further
    // events/reads arrive. The existing delegate keeps its ticket until Run.
    eventQueue.discard(findCapture, false);
  }
  // DevBench installs its interface listener during kPostLoad. All plugins
  // finish that stage before kDataLoaded, avoiding registration-order races.
  if (!registrationGate.ready(m->type == SKSE::MessagingInterface::kDataLoaded))
    return;
  auto *api = DevBenchAPI::GetDevBenchInterface001();
  if (!api || api->GetBuildNumber() < 13500)
    return;
  // Event sinks only once DevBench can reach the tools that read them: without it they are pure cost.
  if (auto *e = RE::ScriptEventSourceHolder::GetSingleton()) {
    e->AddEventSink<RE::TESMagicEffectApplyEvent>(&sinks);
    e->AddEventSink<RE::TESSpellCastEvent>(&sinks);
    e->AddEventSink<RE::TESHitEvent>(&sinks);
  }
  static const char *ops[] = {"look", "near", "ref", "effects", "watch"};
  for (auto *op : ops) {
    auto name = std::string("probe.") + op;
    J desc = {
        {"description", std::string("Read-only ToolkitProbe ") + op +
                            "; experimental, see coverage"},
        {"inputSchema",
         {{"type", "object"},
          {"properties",
           {{"from",
             {{"type", "string"},
              {"enum", J::array({"hmd", "right", "left", "crosshair"})}}},
            {"max", {{"type", "number"}, {"minimum", 0}, {"maximum", 10000}}},
            {"radius",
             {{"type", "number"}, {"minimum", 0}, {"maximum", 10000}}},
            {"at",
             {{"oneOf", J::array({J{{"const", "player"}},
                                  J{{"type", "array"},
                                    {"items", {{"type", "number"}}},
                                    {"minItems", 3},
                                    {"maxItems", 3}}})}}},
            {"types",
             {{"type", "string"},
              {"description", "Comma-separated exact base form-type names"}}},
            {"limit", {{"type", "integer"}, {"minimum", 1}, {"maximum", 100}}},
            {"formId", {{"type", "string"}}},
            {"actor", {{"type", "string"}}},
            {"seconds",
             {{"type", "integer"}, {"minimum", 1}, {"maximum", 600}}},
            {"read", {{"type", "boolean"}}},
            {"stop", {{"type", "boolean"}}},
            {"captureId", {{"type", "string"}}},
            {"after",
             {{"type", "integer"},
              {"minimum", 0},
              {"maximum", 9007199254740991ULL}}}}}}},
        {"readOnly", true}};
    if (!api->RegisterTool(name.c_str(), desc.dump().c_str(), handler,
                           const_cast<char *>(op)))
      SKSE::log::warn(
          "ToolkitProbe: registration replaced an existing {} handler", name);
  }
}
} // namespace
SKSEPluginLoad(const SKSE::LoadInterface *skse) {
  SKSE::Init(skse);
  SKSE::GetMessagingInterface()->RegisterListener(message);
  return true;
}
