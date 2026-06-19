import { NextRequest, NextResponse } from 'next/server'
import { buildKalshiHeaders } from '@/lib/kalshi-auth'
import { KALSHI_BASE } from '@/lib/kalshi'
import { normalizeKalshiOrder } from '@/lib/types'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ orderId: string }> },
) {
  const { orderId } = await params
  const encoded = encodeURIComponent(orderId)
  const path = `/trade-api/v2/portfolio/orders/${encoded}`
  const headers = buildKalshiHeaders('GET', path)

  const res = await fetch(`${KALSHI_BASE}/portfolio/orders/${encoded}`, { headers })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) return NextResponse.json(data, { status: res.status })
  const order = (data as { order?: unknown }).order ?? data
  return NextResponse.json({ ...data, order: normalizeKalshiOrder(order) })
}
