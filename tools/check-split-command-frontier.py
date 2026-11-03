#!/usr/bin/env python3
"""Exercise actual split ring/dispatch/wait methods and the bounded fetch observer."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root/'Eglantine/src/video_core/gpu_split.cpp').read_text()

def method(name):
    start = source.index(name)
    body = source.index('{', start)
    end, depth = body+1, 1
    while depth:
        depth += (source[end] == '{')-(source[end] == '}')
        end += 1
    return source[start:end]

methods = '\n'.join(method(name) for name in [
    'void GpuSplit::Stop()', 'u64* GpuSplit::ReserveSlow(',
    'void GpuSplit::WaitForSpace(', 'void GpuSplit::Publish()',
    'void GpuSplit::Drain()', 'void GpuSplit::DrainCommandReads()',
    'void GpuSplit::DrainTo(', 'void GpuSplit::SwitchEngine(',
    'void GpuSplit::MultiMethod(', 'size_t GpuSplit::Execute(',
    'void GpuSplit::BackLoop('])
front = (root/'Eglantine/src/video_core/engines/maxwell_3d.cpp').read_text()
classify_start = front.index('        const bool may_write = VideoCommon::CommandWriteFrontier::StateWriteMayExecute(')
classify_end = front.index(';', front.index('split.MacroWrite(this, method, argument, may_write)', classify_start))+1
classification = front[classify_start:classify_end]
header = (root/'Eglantine/src/video_core/gpu_split.h').read_text()
assert header.count('private:') == 1
header = header.replace('private:', 'public:')  # Test access only, never the compiled port.
code = r'''
#include <cassert>
#include <chrono>
#include <future>
#include <iostream>
#include <optional>
#include "common/common_types.h"
#include "video_core/nx2.h"
namespace Core {struct System {void RegisterHostThread(){}};}
#include "split-under-test.h"
namespace Common {
enum class ThreadPriority {Critical};
void SetCurrentThreadName(const char*){}
void SetCurrentThreadPriority(ThreadPriority){}
void SetCurrentThreadToPerformanceCores(){}
}
namespace VideoCommon::Nx2Profile {
enum class Zone {SplitDrain,SplitWait,Method};
struct Scope {explicit Scope(Zone){}};
void Report(const char* = nullptr){}
}
std::atomic<bool> narrow{true};
std::atomic<unsigned> trace_begins{},trace_ends{};
extern "C" bool eden_split_fetch_frontier_enabled() __attribute__((weak));
extern "C" u64 eden_split_fetch_begin(u64,u64,u64,unsigned) __attribute__((weak));
extern "C" void eden_split_fetch_end(u64) __attribute__((weak));
extern "C" bool eden_split_fetch_frontier_enabled(){return narrow.load();}
extern "C" u64 eden_split_fetch_begin(u64 target,u64 queued,u64 consumed,unsigned mode){
 assert(target<=queued&&consumed<=queued&&mode==unsigned(narrow.load()));
 if(!mode)assert(target==queued);
 ++trace_begins;return 0;
}
extern "C" void eden_split_fetch_end(u64 index){assert(index==0);++trace_ends;}
namespace VideoCommon {
static std::atomic<bool> g_back_busy{};
static void Pause(){std::this_thread::yield();}
GpuSplit GpuSplit::instance;
''' + methods + r'''
}
using Split=VideoCommon::GpuSplit;
using Clock=std::chrono::steady_clock;
bool await(std::atomic<bool>& flag){
 auto end=Clock::now()+std::chrono::seconds(3);
 while(!flag.load(std::memory_order_acquire)){
  if(Clock::now()>end)return false;
  std::this_thread::yield();
 }
 return true;
}
struct Engine:Tegra::Engines::EngineInterface {
 struct FrontState {bool draw_pending;};
 void Emit(Split& split,FrontState& fs,u32 method,u32 argument){
''' + classification + r'''
 }
 std::atomic<bool> tail_started{},tail_release{};
 std::atomic<unsigned> state{};
 void CallMethod(Core::System&,u32 method,u32 value,bool)override{
  if(method==7){tail_started.store(true,std::memory_order_release);await(tail_release);}
  state.store(value,std::memory_order_release);
 }
 void CallMultiMethod(Core::System& system,u32 method,const u32* values,u32 count,u32)override{
  for(u32 i=0;i<count;++i)CallMethod(system,method,values[i],true);
 }
};
void start(Split& s,Core::System& system){
 s.system=&system;s.active=true;s.ring=std::make_unique<u64[]>(s.RingWords);
 s.back_thread=std::jthread([&s](std::stop_token stop){s.BackLoop(stop);});
}
int main(){
 Core::System system;
 for(bool enabled:{false,true}){
  narrow.store(enabled);Split s;Engine engine;
  std::atomic<bool> writer_started{},writer_release{};
  std::atomic<unsigned> guest{};
  constexpr GPUVAddr address=0x50000;
  Tegra::Nx2ClearGpuWrittenPages();
  start(s,system);
  s.Defer([&]{writer_started.store(true,std::memory_order_release);await(writer_release);
              guest.store(42,std::memory_order_release);Tegra::Nx2MarkGpuWritten(address,4);});
  const u64 producer=s.command_writes.Target();assert(producer>0);
  // This tail only touches engine state. Block it to make early producer completion observable.
  s.MacroWrite(&engine,7,99,false);const u64 queued=s.write_position;
  assert(producer<queued&&s.command_writes.Target()==producer);
  s.Publish();assert(await(writer_started));
  auto waiter=std::async(std::launch::async,[&]{
   s.DrainCommandReads();
   // The first queued writer must have run before historical-page inspection.
   assert(guest.load(std::memory_order_acquire)==42);
   assert(Tegra::Nx2HasGpuWrittenPage(address,4,[](GPUVAddr page){return std::optional{page};}));
  });
  // Observe the actual completion request before releasing the producer. If a
  // request arrives after the back passed it, acknowledgement can conservatively
  // wait until the next published boundary; that timing is not an ordering bypass.
  const auto request_deadline=Clock::now()+std::chrono::seconds(2);
  while(s.completion.requested.load()!= (enabled?producer:queued) && Clock::now()<request_deadline)
   std::this_thread::yield();
  const bool requested=s.completion.requested.load()==(enabled?producer:queued);
  writer_release.store(true,std::memory_order_release);assert(await(engine.tail_started));
  const bool returned=waiter.wait_for(std::chrono::milliseconds(200))==std::future_status::ready;
  engine.tail_release.store(true,std::memory_order_release);
  waiter.get();s.Drain();
  assert(requested&&returned==enabled); // OFF waits for the entire tail; ON acknowledges its actual producer.
  assert(engine.state.load()==99&&s.consumed.load()==queued);
  s.Stop();assert(!s.active&&s.command_writes.Target()==0&&s.write_position==0);
  assert(!s.completion.ShouldPublish(1,0));
 }
 // Classification is conservative by default, including arbitrary deferred callbacks.
 {
  Split s;Engine e;s.ring=std::make_unique<u64[]>(s.RingWords);
  s.Method(&e,5,1,true,nullptr);assert(s.command_writes.Target()==0);
  e.execution_mask[6]=true;s.Method(&e,6,1,true,nullptr);
  assert(s.command_writes.Target()==s.write_position);
  s.MacroWrite(&e,5,2);assert(s.command_writes.Target()==s.write_position);
  s.Defer([]{});assert(s.command_writes.Target()==s.write_position);
  const auto callback=s.command_writes.Target();
  for(unsigned i=0;i<100;++i)s.MacroWrite(&e,5,i,false);
  assert(s.command_writes.Target()==callback);
  const u32 values[]{1,2,3};s.MultiMethod(&e,5,values,3,3,nullptr);
  assert(s.command_writes.Target()==s.write_position);
 }
 // Exercise the actual FrontWrite call-site classification against the actual ring.
 {
  Split s;Engine e;s.ring=std::make_unique<u64[]>(s.RingWords);
  for(unsigned mask=0;mask<4;++mask){
   e.execution_mask[5]=bool(mask&1);Engine::FrontState fs{bool(mask&2)};
   const auto previous=s.command_writes.Target();
   e.Emit(s,fs,5,123);
   if(mask)assert(s.command_writes.Target()==s.write_position);
   else assert(s.command_writes.Target()==previous);
  }
 }
 // The public state-write predicate must retain possible deferred draws.
 using Frontier=VideoCommon::CommandWriteFrontier;
 for(unsigned mask=0;mask<4;++mask){
  bool state_only=mask&1,pending=mask&2;
  assert(Frontier::StateWriteMayExecute(state_only,pending)==(!state_only||pending));
 }
 assert(trace_begins==trace_ends&&trace_begins==2);
 std::cout<<"Actual split ring PASS: first queued writer, state tail, OFF/ON ordering, "
              "mid-batch completion, unknown callbacks/multi writes and restart reset\n";
}
'''
with tempfile.TemporaryDirectory(prefix='split-frontier-') as temporary:
    folder=Path(temporary)
    (folder/'split-under-test.h').write_text(header)
    helper = folder/'video_core/split_completion.h'
    helper.parent.mkdir()
    helper.write_text((root/'Eglantine/src/video_core/split_completion.h').read_text().replace('private:', 'public:'))
    (folder/'check.cpp').write_text(code)
    subprocess.run(['clang++-18','-std=c++20','-O1','-g','-pthread',
                    '-fsanitize=address,undefined','-fno-omit-frame-pointer',
                    '-I'+str(folder),'-I'+str(root/'Eglantine/src'),
                    str(folder/'check.cpp'),'-o',str(folder/'check')],check=True)
    subprocess.run([str(folder/'check')],check=True,timeout=15)
