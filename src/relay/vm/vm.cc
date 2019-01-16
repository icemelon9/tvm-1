/*!
 *  Copyright (c) 2018 by Contributors
 * \file tvm/relay/runtime/runtime.h
 * \brief Abstract device memory management API
 */

#include <tvm/relay/expr_functor.h>
#include <tvm/relay/vm/vm.h>
#include <tvm/relay/interpreter.h>
#include "../backend/compile_engine.h"

#include <vector>
#include <iostream>

namespace tvm {
namespace relay {
namespace vm {

Instruction::Instruction() {}

Instruction::Instruction(const Instruction& instr) {
  this->op = instr.op;
  switch (instr.op) {
    case Opcode::Push:
      this->stack_index = instr.stack_index;
      return;
    case Opcode::Ret:
      return;
    case Opcode::AllocTensor:
      this->tensor_info = instr.tensor_info;
      return;
    case Opcode::InvokePacked:
      this->packed_index = instr.packed_index;
      this->arity = instr.arity;
      return;
    case Opcode::If:
      this->true_offset = instr.true_offset;
      this->false_offset = instr.false_offset;
      return;
    case Opcode::Invoke:
      this->func_index = instr.func_index;
      return;
  }
}

// TODO(@jroesch): this leaks memory fix me
Instruction::~Instruction() {}

Instruction Push(size_t stack_index) {
  Instruction instr;
  instr.op = Opcode::Push;
  instr.stack_index = stack_index;
  return instr;
}

Instruction Ret() {
  Instruction instr;
  instr.op = Opcode::Ret;
  return instr;
}

Instruction InvokePacked(size_t packed_index, size_t arity) {
  Instruction instr;
  instr.op = Opcode::InvokePacked;
  instr.packed_index = packed_index;
  instr.arity = arity;
  return instr;
}

Instruction AllocTensor(const std::vector<int64_t> shape, DLDataType dtype) {
  Instruction instr;
  instr.op = Opcode::AllocTensor;
  instr.tensor_info.shape = new int64_t[shape.size()];
  instr.tensor_info.ndim = shape.size();
  std::memcpy(
      reinterpret_cast<void*>(instr.tensor_info.shape),
      reinterpret_cast<const void*>(shape.data()),
      shape.size() * sizeof(int64_t));
  instr.tensor_info.dtype = dtype;
  return instr;
}

Instruction If(size_t true_branch, size_t false_branch) {
  Instruction instr;
  instr.op = Opcode::If;
  instr.true_offset = true_branch;
  instr.false_offset = false_branch;
  return instr;
}

void InstructionPrint(std::ostream& os, const Instruction& instr) {
  switch (instr.op) {
    case Opcode::Push: {
      os << "push " << instr.stack_index;
      break;
    }
    case Opcode::Ret: {
      os << "ret";
      break;
    }
    case Opcode::InvokePacked: {
      os << "invoke_packed ";
      os << instr.packed_index;
      os << " " << instr.arity;
      break;
    }
    case Opcode::AllocTensor: {
      os << "alloc_tensor";
      os << "(";

      for (size_t i = 0; i < instr.tensor_info.ndim; i++) {
        os << instr.tensor_info.shape[i] << ", ";
      }
      os << ") ";
      os << TVMType2Type(instr.tensor_info.dtype);
      break;
    }
    case Opcode::If: {
      os << "if "
         << instr.true_offset << " "
         << instr.false_offset;
    }
    case Opcode::Invoke: {
      os << "invoke "
         << instr.false_offset;
    }
  }
}

void VMFunctionPrint(const VMFunction& vm_func) {
  for (auto instr : vm_func.instructions) {
    InstructionPrint(std::cout, instr);
    std::cout << ";" << std::endl;
  }
}

struct VMCompiler : ExprFunctor<void(const Expr& expr)> {
    std::vector<Instruction> instructions;
    std::unordered_map<Var, size_t, NodeHash, NodeEqual> var_map;
    size_t stack_index;
    bool seen_func;
    CompileEngine engine;
    std::vector<LoweredFunc> lowered_funcs;

    VMCompiler() :
      instructions(), var_map(), stack_index(0),
      seen_func(false), engine(CompileEngine::Global()) {}

    inline void Emit(const Instruction& instr) {
      instructions.push_back(instr);
    }

    void VisitExpr_(const VarNode* var_node) {
      auto var = GetRef<Var>(var_node);
      auto it = this->var_map.find(var);
      CHECK(it != this->var_map.end());
      this->instructions.push_back(Push(it->second));
    }

    void VisitExpr_(const IfNode* if_node) {
      this->VisitExpr(if_node->cond);
      auto after_cond = this->instructions.size();
      this->Emit(If(0, 0));
      this->VisitExpr(if_node->true_branch);
      auto after_true = this->instructions.size();
      this->VisitExpr(if_node->false_branch);
      // After we emit the true body, and false body,
      // we patch up the if instruction.
      auto true_offset = 1;
      auto false_offset = after_true - after_cond;
      this->instructions[after_cond].true_offset = true_offset;
      this->instructions[after_cond].false_offset = false_offset;

    }

    void VisitExpr_(const CallNode* call_node) {
      auto func_node = call_node->op.as<FunctionNode>();
      CHECK(func_node);

      std::cout << "Pushing Arguments on to stack" << std::endl;
      // First generate instructions to populate stack with arguments.
      std::cout << call_node->args << std::endl;
      for (auto arg : call_node->args) {
        this->VisitExpr(arg);
      }

      std::cout << "Allocating space for return value" << std::endl;
      // Allocate space for the return tensor.
      Type rtype = call_node->checked_type();
      std::cout << "Return type: " << rtype << std::endl;
      const TensorTypeNode* ttype = rtype.as<TensorTypeNode>();
      CHECK(ttype);

      std::vector<int64_t> shapes;
      std::cout << "generating shape" << std::endl;
      for (auto sh : ttype->shape) {
        shapes.push_back(Downcast<tvm::Integer>(sh)->value);
      }

      DataType dtype = ttype->dtype;
      TVMType dltype = Type2TVMType(dtype);
      Instruction alloc = AllocTensor(shapes, dltype);
      Emit(alloc);

      std::cout << "Emit invoke" << std::endl;
      // Next generate the invoke instruction.
      CHECK(func_node->IsPrimitive());
      auto target = Target::create("llvm");
      auto key = CCacheKeyNode::make(GetRef<Function>(func_node), target);
      auto cfunc = engine->Lower(key);
      // TODO: support lowered funcs for multiple targets
      CHECK(cfunc->funcs.size() == 1);
      auto op_index = this->lowered_funcs.size();
      this->lowered_funcs.push_back(cfunc->funcs[0]);
      // TODO(@jroesch): this doesn't support tuples right now.
      size_t arity = func_node->params.size() + 1;
      CHECK(arity < 10);
      Emit(InvokePacked(op_index, arity));
    }

    void VisitExpr_(const FunctionNode* func_node) {
      CHECK(!seen_func);
      this->seen_func = true;
      for (auto param : func_node->params) {
        var_map.insert({ param, this->stack_index++ });
      }

      this->VisitExpr(func_node->body);
    }
};

using CompiledFunc = std::pair<std::vector<LoweredFunc>, VMFunction>;

void PopulatePackedFuncMap(
  const std::vector<LoweredFunc>& lowered_funcs,
  std::vector<PackedFunc>* packed_funcs) {
  runtime::Module mod;
  if (lowered_funcs.size() > 0) {
    // TODO(@jroesch): we need to read target from build config
    Target target = Target::create("llvm");
    if (const auto* f = runtime::Registry::Get("relay.backend.build")) {
      mod = (*f)(tvm::Array<LoweredFunc>(lowered_funcs.begin(), lowered_funcs.end()), target);
    } else {
      LOG(FATAL) << "relay.backend.build is not registered";
    }
    CHECK(mod.operator->());
    for (auto lfunc : lowered_funcs) {
      packed_funcs->push_back(mod.GetFunction(lfunc->name));
    }
  }
}

CompiledFunc CompileFunc(const Function& func) {
  size_t params = func->params.size();
  VMCompiler compiler;
  compiler.VisitExpr(func);
  compiler.instructions.push_back(Ret());
  VMFunction vm_func = VMFunction(params, compiler.instructions);
  return { compiler.lowered_funcs, vm_func };
}

VirtualMachine CompileModule(const Module& mod) {
  VirtualMachine vm;
  std::vector<LoweredFunc> lowered_funcs;

  for (auto named_func : mod->functions) {
    auto gvar = named_func.first;
    auto func = named_func.second;
    auto cfunc = CompileFunc(func);
    auto lfuncs = cfunc.first;
    auto vm_func = cfunc.second;

    lowered_funcs.insert(
      lowered_funcs.end(),
      lfuncs.begin(),
      lfuncs.end());

    vm.functions.push_back(vm_func);
  }

  PopulatePackedFuncMap(lowered_funcs, &vm.packed_funcs);

  return vm;
}

void VirtualMachine::PushFrame(size_t arg_count, size_t ret_pc, const VMFunction& vm_func) {
  auto frame = VMFrame(ret_pc, bp, func_index, arg_count, code);
  frames.push_back(frame);
  std::cout << "initial stack size" << stack.size() << std::endl;
}

size_t VirtualMachine::PopFrame() {
  CHECK(frames.size() != 0);
  const VMFrame& fr = frames.back();
  auto stack_size = stack.size();
  // Copy return value;
  CHECK(stack_size - fr.args - 1 < stack.size())
    << "attempting to read stack slot: "
    << stack_size - fr.args - 1
    << " stack_size: "
    << stack_size;

  CHECK(0 <= stack_size - fr.args - 1);
  stack[stack_size - fr.args - 1] = stack[stack_size - 1];
  // Resize value stack.
  stack.resize(stack_size - fr.args);
  // Reset frame.
  bp = fr.bp;
  pc = fr.pc;
  func_index = fr.func_index;
  code = fr.code;
  auto call_stack_size = frames.size();
  frames.pop_back();
  return call_stack_size;
}

void VirtualMachine::InvokeGlobal(const VMFunction& func, const std::vector<VMObject>& args) {
  auto stack_start = stack.size();
  stack.push_back(VMObject());
  for (auto arg : args) {
    stack.push_back(arg);
  }
  PushFrame(func.params, this->pc + 1, func);
  CHECK(stack_start + func.params + 1 == stack.size());
  code = func.instructions.data();
  pc = 0;
  bp = stack.size() - func.params;
  std::cout << "final stack size: " << stack.size() << "bp: " << bp << std::endl;
}

VMObject VirtualMachine::Invoke(const VMFunction& func, const std::vector<VMObject>& args) {
  InvokeGlobal(func, args);
  Run();
  std::cout << "final stack size: " << stack.size() << "bp: " << bp << std::endl;
  return stack.back();
}

void InvokePacked(const PackedFunc& func, size_t arg_count, std::vector<VMObject>& stack) {
  CHECK(arg_count <= stack.size());

  std::vector<TVMValue> values(arg_count);
  std::vector<int> codes(arg_count);
  runtime::TVMArgsSetter setter(values.data(), codes.data());

  std::cout << "InvokePacked: " << stack.size() << std::endl;

  auto stack_start = stack.size() - arg_count;
  for (size_t i = 0; i < arg_count; i++) {
    std::cout << "Getting: " << stack_start + i << std::endl;
    NDArray data = ToNDArray(stack[stack_start + i]);
    setter(i, data);
  }

  TVMRetValue rv;
  func.CallPacked(TVMArgs(values.data(), codes.data(), arg_count), &rv);
  // We can do this more efficiently by reverse laying out the arguments
  // and just shrinking the stack.
  stack[stack.size() - arg_count] = stack[stack.size() - 1];
  stack.resize(stack.size() - arg_count + 1);
}

void VirtualMachine::Run() {
  CHECK(this->code);
  this->pc = 0;
  auto stack_start = frames.size();
  while (true) {
  main_loop:
    auto const& instr = this->code[this->pc];
    std::cout << "Executing: ";
    InstructionPrint(std::cout, instr);
    std::cout << std::endl;
    std::cout << "Stack Size: " << stack.size() << std::endl;
    switch (instr.op) {
      case Opcode::Invoke: {
        LOG(FATAL) << "false";
      }
      case Opcode::InvokePacked: {
        auto start_stack = stack.size();
        const auto& func = packed_funcs[instr.packed_index];
        const auto& arity = instr.arity;
        std::cout << "before call" << std::endl;
        std::cout << this->stack.size() << std::endl;
        InvokePacked(func, arity, stack);
        CHECK(start_stack - arity + 1 == stack.size())
          << "start_stack: " << start_stack
          << "end_stack: " << stack.size();
        std::cout << "after call" << std::endl;
        pc++;
        goto main_loop;
      }
      case Opcode::If: {
        // How do we do this efficiently?
        DLContext cpu_ctx;
        cpu_ctx.device_type = kDLCPU;
        cpu_ctx.device_id = 0;

        const auto& cond = stack.back();
        NDArray cpu_array = ToNDArray(cond).CopyTo(cpu_ctx);
        CHECK_EQ(TVMType2Type(cpu_array->dtype), Bool());
        bool branch = reinterpret_cast<uint8_t*>(cpu_array->data)[0];

        // Remove cond.
        stack.pop_back();

        if (branch) {
          pc += instr.true_offset;
        } else {
          pc += instr.false_offset;
        }

        goto main_loop;
      }
      case Opcode::AllocTensor: {
        const auto& ti = instr.tensor_info;
        DLContext ctx;
        ctx.device_type = DLDeviceType::kDLCPU;
        ctx.device_id = 0;
        auto shape = std::vector<int64_t>(ti.ndim);
        shape.assign(ti.shape, ti.shape + ti.ndim);
        auto data = NDArray::Empty(shape, ti.dtype, ctx);
        stack.push_back(VMTensor(data));
        pc++;
        goto main_loop;
      }
      case Opcode::Push: {
        CHECK(bp + instr.stack_index < stack.size());
        stack.push_back(stack[bp + instr.stack_index]);
        pc++;
        goto main_loop;
      }
      case Opcode::Ret: {
        // If we have hit the point from which we started
        // running, we should return to the caller breaking
        // the dispatch loop.
        if (PopFrame() == stack_start) {
          return;
        // Otherwise we are just returning from a local call.
        //
        // Since we have already popped the stack we will just
        // resume at the top of the dispatch loop.
        } else {
          goto main_loop;
        }
      }
    }
  }
}

TVM_REGISTER_API("relay._runtime._testeval")
.set_body([](TVMArgs args, TVMRetValue* ret) {
    NodeRef to_compile = args[0];

    Module module;
    if (to_compile.as<FunctionNode>()) {
      Function to_compile = args[0];
      module = ModuleNode::FromExpr(to_compile);
    } else if (to_compile.as<ModuleNode>()) {
      module = args[0];
    } else {
      LOG(FATAL) << "expected function or module";
    }

    tvm::Array<Value> vargs = args[1];

    VirtualMachine vm = CompileModule(module);
    VMFunctionPrint(vm.functions[0]);
    std::cout << "Before convert" << std::endl;

    std::vector<VMObject> vm_args;
    for (auto arg : vargs) {
      auto tvarg = Downcast<TensorValue>(arg);
      vm_args.push_back(VMTensor(tvarg->data));
    }

    VMObject result = vm.Invoke(vm.functions[0], vm_args);

    // Directly returning ndarray causes segfault.
    NDArray nd = ToNDArray(result);
    std::cout << "Getting ND finished";
    *ret = TensorValueNode::make(nd);
});


}  // namespace vm
}  // namespace relay
}  // namespace tvm
