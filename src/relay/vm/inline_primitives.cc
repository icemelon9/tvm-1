/*!
 *  Copyright (c) 2018 by Contributors
 * \file tvm/relay/vm/inline_primitives.cc
 * \brief Ensure that primitives only appear in the call position.
 */

#include <tvm/runtime/memory_manager.h>
#include <tvm/relay/expr_functor.h>
#include <tvm/relay/vm/vm.h>
#include <tvm/relay/logging.h>
#include <tvm/relay/interpreter.h>
#include <vector>
#include <iostream>
#include "../backend/compile_engine.h"
#include "../../runtime/naive_allocator.h"

using namespace tvm::runtime;

namespace tvm {
namespace relay {
namespace vm {

struct PrimitiveInliner : ExprMutator {
    Module module_;
    std::unordered_map<Var, Expr, NodeHash, NodeEqual> var_map;

    explicit PrimitiveInliner(const Module& module) : module_(module) {}

    Expr VisitExpr_(const LetNode* let_node) {
        var_map.insert({let_node->var, VisitExpr(let_node->value) });
        return ExprMutator::VisitExpr_(let_node);
    }

    Expr VisitExpr_(const CallNode* call) {
        Expr op = call->op;
        // For now just collapse the chain of variables to see if
        // they point to a primitive function.
        const VarNode* var_node;

        while ((var_node = op.as<VarNode>())) {
            auto var = GetRef<Var>(var_node);
            RELAY_LOG(INFO) << "Var: " << var << std::endl;
            auto it = var_map.find(GetRef<Var>(var_node));
            if (it != var_map.end()) {
                op = it->second;
            } else {
                return ExprMutator::VisitExpr_(call);
            }
        }

        if (auto func = op.as<FunctionNode>()) {
            if (func->IsPrimitive()) {
                return CallNode::make(
                    GetRef<Function>(func),
                    call->args,
                    call->attrs,
                    call->type_args);
            }
        }

        if (auto global = op.as<GlobalVarNode>()) {
            return CallNode::make(
                GetRef<GlobalVar>(global),
                call->args,
                call->attrs,
                call->type_args);
        }

        return ExprMutator::VisitExpr_(call);
    }

    Expr VisitExpr_(const FunctionNode* func) {
        if (func->IsPrimitive()) {
            return GetRef<Function>(func);
        } else {
            return ExprMutator::VisitExpr_(func);
        }
    }

    Function Inline(const Function& func) {
        RELAY_LOG(INFO) << "Inline "
            << std::endl
            << "func= " << AsText(func, false)
            << std::endl;

        auto inlined = FunctionNode::make(
            func->params,
            DeadCodeElimination(VisitExpr(func->body)),
            func->ret_type,
            func->type_params,
            func->attrs);

        RELAY_LOG(INFO) << "Inline "
            << std::endl
            << "after_func= " << AsText(inlined, false)
            << std::endl;;
        return inlined;
    }
};

// TODO(@jroesch): write verifier

/* This pass will eliminate primitives which have been lifted by the ANF
 * transform inlining them directly into call sites.
 *
 * This makes VM related code generation easier as the call target is always
 * a primitive function.
 *
 * let prim = fn(...) { ... };
 * prim(...)
 *
 * will become:
 *
 * (fn(...) { ... })(...)
 */
Module InlinePrimitives(const Module& module) {
    PrimitiveInliner inliner(module);

    tvm::Map<GlobalVar, Function> updates;

    // There is an ordering bug here.
    for (auto pair : module->functions) {
      auto global = pair.first;
      auto func = pair.second;
      updates.Set(global, inliner.Inline(func));
    }

    for (auto pair : updates) {
      module->Add(pair.first, pair.second, true);
    }

    return module;
}

}  // namespace vm
}  // namespace relay
}  // namespace tvm
