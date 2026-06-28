import { Router } from 'express';
import { db } from '../db/database';
import { requireAuth } from '../middleware/auth';
import type { RunResult } from '../types';

const router = Router();

function getOrderWithItems(orderId: number, userId?: number) {
  const whereUser = userId !== undefined ? 'AND o.user_id=?' : '';
  const params: any[] = userId !== undefined ? [orderId, userId] : [orderId];
  const order = db.prepare(`SELECT * FROM orders o WHERE o.id=? ${whereUser}`).get(...params) as any;
  if (!order) return null;
  const items = db.prepare(`
    SELECT oi.*, p.name, p.sku, p.image_url
    FROM order_items oi JOIN products p ON p.id=oi.product_id
    WHERE oi.order_id=?
  `).all(orderId) as any[];
  order.items = items;
  order.shipping_address = JSON.parse(order.shipping_address_json || '{}');
  return order;
}

// POST /orders — checkout
router.post('/', requireAuth, (req, res) => {
  const userId = req.user!.userId;
  const { shipping_name, shipping_address } = req.body || {};
  if (!shipping_name || !shipping_address) {
    return res.status(400).json({ error: 'shipping_name and shipping_address required' });
  }

  const cartItems = db.prepare(`
    SELECT ci.product_id, ci.quantity, p.price, p.name
    FROM cart_items ci JOIN products p ON p.id=ci.product_id
    WHERE ci.user_id=? AND p.active=1
  `).all(userId) as any[];

  if (!cartItems.length) return res.status(400).json({ error: 'Cart is empty' });

  // Atomic inventory check and decrement for each item
  for (const item of cartItems) {
    const result = db.prepare(
      'UPDATE inventory SET quantity=quantity-?, updated_at=datetime(\'now\') WHERE product_id=? AND quantity>=?'
    ).run(item.quantity, item.product_id, item.quantity) as unknown as RunResult;
    if (result.changes === 0) {
      return res.status(400).json({ error: `Insufficient stock for: ${item.name}` });
    }
  }

  const subtotal = cartItems.reduce((sum, i) => sum + i.price * i.quantity, 0);
  const tax = parseFloat((subtotal * 0.07).toFixed(2));
  const total = parseFloat((subtotal + tax).toFixed(2));
  const paymentRef = `MOCK-${Date.now()}-${Math.random().toString(36).slice(2, 8).toUpperCase()}`;

  const orderResult = db.prepare(
    'INSERT INTO orders (user_id,status,subtotal,tax,total,shipping_name,shipping_address_json,payment_ref) VALUES (?,?,?,?,?,?,?,?)'
  ).run(userId, 'pending', subtotal, tax, total, shipping_name, JSON.stringify(shipping_address), paymentRef) as unknown as RunResult;

  const orderId = orderResult.lastInsertRowid;
  for (const item of cartItems) {
    db.prepare('INSERT INTO order_items (order_id,product_id,quantity,unit_price) VALUES (?,?,?,?)').run(orderId, item.product_id, item.quantity, item.price);
  }

  // Clear cart
  db.prepare('DELETE FROM cart_items WHERE user_id=?').run(userId);

  const order = getOrderWithItems(orderId);
  return res.status(201).json({ order });
});

// GET /orders — user's orders
router.get('/', requireAuth, (req, res) => {
  const userId = req.user!.userId;
  const orders = db.prepare('SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC').all(userId) as any[];
  return res.json({ orders });
});

// GET /orders/:id
router.get('/:id', requireAuth, (req, res) => {
  const id = parseInt(req.params.id, 10);
  const userId = req.user!.userId;
  const isAdmin = req.user!.role === 'admin';
  const order = getOrderWithItems(id, isAdmin ? undefined : userId);
  if (!order) return res.status(404).json({ error: 'Order not found' });
  return res.json({ order });
});

export default router;
