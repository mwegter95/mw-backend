import { Router } from 'express';
import { db } from '../db/database';
import { requireAuth } from '../middleware/auth';
import type { RunResult } from '../types';

const router = Router();

function getCart(userId: number) {
  return db.prepare(`
    SELECT ci.id, ci.product_id, ci.quantity,
           p.name, p.price, p.image_url, p.sku, p.slug,
           i.quantity as stock
    FROM cart_items ci
    JOIN products p ON p.id = ci.product_id
    LEFT JOIN inventory i ON i.product_id = ci.product_id
    WHERE ci.user_id = ? AND p.active = 1
  `).all(userId) as any[];
}

// GET /cart
router.get('/', requireAuth, (req, res) => {
  const items = getCart(req.user!.userId);
  return res.json({ items });
});

// POST /cart/items
router.post('/items', requireAuth, (req, res) => {
  const { product_id, quantity = 1 } = req.body || {};
  if (!product_id) return res.status(400).json({ error: 'product_id required' });
  const qty = Math.max(1, parseInt(quantity, 10) || 1);

  const product = db.prepare('SELECT id FROM products WHERE id=? AND active=1').get(product_id) as any;
  if (!product) return res.status(404).json({ error: 'Product not found' });

  const stock = db.prepare('SELECT quantity FROM inventory WHERE product_id=?').get(product_id) as any;
  if (stock && stock.quantity < qty) {
    return res.status(400).json({ error: 'Insufficient stock' });
  }

  db.prepare(`
    INSERT INTO cart_items (user_id,product_id,quantity) VALUES (?,?,?)
    ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=quantity+excluded.quantity
  `).run(req.user!.userId, product_id, qty);

  const items = getCart(req.user!.userId);
  return res.json({ items });
});

// PUT /cart/items/:id
router.put('/items/:id', requireAuth, (req, res) => {
  const id = parseInt(req.params.id, 10);
  const { quantity } = req.body || {};
  const qty = parseInt(quantity, 10);
  if (!qty || qty < 1) return res.status(400).json({ error: 'quantity must be >= 1' });

  const item = db.prepare('SELECT * FROM cart_items WHERE id=? AND user_id=?').get(id, req.user!.userId) as any;
  if (!item) return res.status(404).json({ error: 'Cart item not found' });

  db.prepare('UPDATE cart_items SET quantity=? WHERE id=?').run(qty, id);
  const items = getCart(req.user!.userId);
  return res.json({ items });
});

// DELETE /cart/items/:id
router.delete('/items/:id', requireAuth, (req, res) => {
  const id = parseInt(req.params.id, 10);
  const r = db.prepare('DELETE FROM cart_items WHERE id=? AND user_id=?').run(id, req.user!.userId) as unknown as RunResult;
  if (r.changes === 0) return res.status(404).json({ error: 'Cart item not found' });
  const items = getCart(req.user!.userId);
  return res.json({ items });
});

// DELETE /cart (clear all)
router.delete('/', requireAuth, (req, res) => {
  db.prepare('DELETE FROM cart_items WHERE user_id=?').run(req.user!.userId);
  return res.json({ items: [] });
});

export default router;
