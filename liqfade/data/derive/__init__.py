"""Tables derived from other tables in the lake, at no cost and with no network.

Everything here rebuilds one canonical table from another, so a parsing rule that turns
out wrong is fixed by re-running rather than by buying bytes a second time. Same spirit
as `liqfade backfill rederive`, which rebuilds `trades` and `liquidations` from
`fills_full`.
"""
