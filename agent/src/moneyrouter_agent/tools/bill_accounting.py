"""Net consumption, attributed to original purchase month; preserve original ledger."""
from dataclasses import replace


def net_rows(rows):
    result = [replace(r, gross_amount_cents=r.amount_cents, original_direction=r.direction,
                      review_reason=r.review_reason or ("原单存在退款状态，未关联退款明细，待复核" if r.refund_like and r.direction == "expense" else "")) for r in rows]
    expenses = [i for i, r in enumerate(rows) if r.direction == "expense" and r.platform_category != "退款"]
    for i in expenses:
        if rows[i].status in ("已全额退款", "全额退款"):
            result[i] = replace(result[i], amount_cents=0, review_reason=rows[i].review_reason,
                                accounting_note="平台明确全额退款，净消费为零；原始流水保留")
    refunded = {}
    links = {}
    for i, row in enumerate(rows):
        is_refund = row.direction in ("income", "transfer") and ("退款" in row.kind or row.platform_category == "退款" or row.status == "退款成功")
        if not is_refund:
            continue
        candidates = [j for j in expenses if rows[j].source == row.source and (
            (row.original_transaction_id and row.original_transaction_id == rows[j].transaction_id) or
            (row.merchant_order_id and row.merchant_order_id == rows[j].merchant_order_id)
        )]
        matched = len(candidates) == 1
        j = candidates[0] if matched else None
        if j is not None and row.date < rows[j].date:
            matched = False
        if matched:
            refunded[j] = refunded.get(j, 0) + row.amount_cents
            links[i] = j
        result[i] = replace(result[i], direction="transfer", category="", accounting_note="退款不作为经营或工资收入",
                            review_reason="" if matched else "退款缺少唯一原单关联，待复核；未自动冲减支出")
    for j, amount in refunded.items():
        if amount > rows[j].amount_cents:
            for i, original in links.items():
                if original == j:
                    result[i] = replace(result[i], review_reason="累计退款超过原单，待复核；未自动冲减支出")
            result[j] = replace(result[j], review_reason="累计退款超过原单，待复核")
            continue
        if rows[j].status in ("已全额退款", "全额退款") and amount < rows[j].amount_cents:
            result[j] = replace(result[j], review_reason="全额退款状态与已关联退款金额不一致，待复核")
            continue
        result[j] = replace(result[j], amount_cents=rows[j].amount_cents - amount,
                            accounting_note=f"关联退款 {amount} 分，归入原消费月份",
                            review_reason=rows[j].review_reason)
    return result
