import { Router } from 'express';
import { db } from '../db/database';
import { requireAdmin } from '../middleware/auth';
import type { RunResult } from '../types';

const router = Router();
router.use(requireAdmin);

// GET /admin/stats
router.get('/stats', (_req, res) => {
  const totalOrders = (db.prepare('SELECT COUNT(*) as c FROM orders').get() as any).c;
  const totalRevenue = (db.prepare("SELECT COALESCE(SUM(total),0) as r FROM orders WHERE status!='cancelled'").get() as any).r;
  const pendingOrders = (db.prepare("SELECT COUNT(*) as c FROM orders WHERE status='pending'").get() as any).c;
  const processingOrders = (db.prepare("SELECT COUNT(*) as c FROM orders WHERE status='processing'").get() as any).c;
  const lowStockCount = (db.prepare('SELECT COUNT(*) as c FROM inventory WHERE quantity<=low_stock_threshold').get() as any).c;
  const totalProducts = (db.prepare('SELECT COUNT(*) as c FROM products WHERE active=1').get() as any).c;
  const totalCustomers = (db.prepare("SELECT COUNT(*) as c FROM users WHERE role='customer'").get() as any).c;

  const recentOrders = db.prepare(`
    SELECT o.id, o.status, o.total, o.created_at, u.name as customer_name, u.email as customer_email
    FROM orders o JOIN users u ON u.id=o.user_id
    ORDER BY o.created_at DESC LIMIT 5
  `).all() as any[];

  const revenueLast7 = db.prepare(`
    SELECT DATE(created_at) as date, SUM(total) as revenue, COUNT(*) as orders
    FROM orders
    WHERE created_at >= datetime('now', '-7 days') AND status != 'cancelled'
    GROUP BY DATE(created_at)
    ORDER BY date ASC
  `).all() as any[];

  return res.json({
    stats: { totalOrders, totalRevenue, pendingOrders, processingOrders, lowStockCount, totalProducts, totalCustomers },
    recentOrders,
    revenueLast7,
  });
});

// GET /admin/inventory
router.get('/inventory', (_req, res) => {
  const items = db.prepare(`
    SELECT p.id, p.name, p.sku, p.price, p.active, c.name as category_name,
           i.quantity, i.low_stock_threshold, i.updated_at
    FROM inventory i
    JOIN products p ON p.id=i.product_id
    JOIN categories c ON c.id=p.category_id
    ORDER BY i.quantity ASC, p.name ASC
  `).all() as any[];
  return res.json({ inventory: items });
});

// PUT /admin/inventory/:pid
router.put('/inventory/:pid', (req, res) => {
  const pid = parseInt(req.params.pid, 10);
  const { quantity, low_stock_threshold } = req.body || {};

  const current = db.prepare('SELECT * FROM inventory WHERE product_id=?').get(pid) as any;
  if (!current) return res.status(404).json({ error: 'Inventory record not found' });

  const newQty = quantity !== undefined ? Math.max(0, parseInt(quantity, 10)) : current.quantity;
  const newThreshold = low_stock_threshold !== undefined ? Math.max(0, parseInt(low_stock_threshold, 10)) : current.low_stock_threshold;

  db.prepare("UPDATE inventory SET quantity=?, low_stock_threshold=?, updated_at=datetime('now') WHERE product_id=?").run(newQty, newThreshold, pid);
  const updated = db.prepare('SELECT * FROM inventory WHERE product_id=?').get(pid) as any;
  return res.json({ inventory: updated });
});

// GET /admin/orders
router.get('/orders', (req, res) => {
  const { status, page = '1', limit = '20' } = req.query as Record<string, string>;
  const pageNum = Math.max(1, parseInt(page, 10) || 1);
  const limitNum = Math.min(100, Math.max(1, parseInt(limit, 10) || 20));
  const offset = (pageNum - 1) * limitNum;

  const conditions: string[] = [];
  const params: any[] = [];
  if (status) {
    conditions.push('o.status=?');
    params.push(status);
  }
  const where = conditions.length ? 'WHERE ' + conditions.join(' AND ') : '';

  const total = (db.prepare(`SELECT COUNT(*) as c FROM orders o ${where}`).get(...params) as any).c;
  const orders = db.prepare(`
    SELECT o.*, u.name as customer_name, u.email as customer_email
    FROM orders o JOIN users u ON u.id=o.user_id
    ${where}
    ORDER BY o.created_at DESC
    LIMIT ? OFFSET ?
  `).all(...params, limitNum, offset) as any[];

  return res.json({ orders, pagination: { page: pageNum, limit: limitNum, total, totalPages: Math.ceil(total / limitNum) } });
});

// PUT /admin/orders/:id/status
router.put('/orders/:id/status', (req, res) => {
  const id = parseInt(req.params.id, 10);
  const { status } = req.body || {};
  const valid = ['pending', 'processing', 'shipped', 'delivered', 'cancelled'];
  if (!valid.includes(status)) {
    return res.status(400).json({ error: `status must be one of: ${valid.join(', ')}` });
  }
  const r = db.prepare('UPDATE orders SET status=? WHERE id=?').run(status, id) as unknown as RunResult;
  if (r.changes === 0) return res.status(404).json({ error: 'Order not found' });
  const order = db.prepare('SELECT * FROM orders WHERE id=?').get(id) as any;
  return res.json({ order });
});

export default router;
