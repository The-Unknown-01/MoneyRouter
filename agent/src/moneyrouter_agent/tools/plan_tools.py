"""Planning tools inspect facts or validate Agent proposals; never choose allocations."""
from langchain_core.tools import tool
from ..domain.wallet import WalletProposal, planning_facts, validate_wallets

TOOL_NAMES = ("inspect_planning_facts", "find_sources", "evaluate_candidate")

def make_plan_tools(context):
    @tool
    def inspect_planning_facts() -> dict:
        """读取确认画像、本月已花和待支付义务、环境变化、历史经验。历史金额不直接作为预算。"""
        return planning_facts(context.inputs)

    @tool
    def find_sources() -> dict:
        """读取有日期与出处的金融环境；与用户不相关的新闻无需引用。"""
        briefing = context.inputs.briefing
        return briefing.model_dump(mode="json") if briefing else {"available": False}

    @tool
    def evaluate_candidate(proposal: WalletProposal) -> dict:
        """核对你自主提出的完整钱包方案，返回金额平衡、义务、期限、风险与来源问题；不修改或保存配置。"""
        return validate_wallets(proposal, context.inputs)

    return [inspect_planning_facts, find_sources, evaluate_candidate]
