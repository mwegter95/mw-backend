import { Router } from 'express';
import { db } from '../db/database';
import { requireAdmin } from '../middleware/auth';
import type { RunResult } from '../types';

const router = Router();

// GET /products?category=&search=&page=1&limit=12&featured=true
router.get('/', (req, res) => {
  const { category, search, page = '1', limit = '12', featured } = req.query as Record<string, string>;
  const pageNum = Math.max(1, parseInt(page, 10) || 1);
  const limitNum = Math.min(50, Math.max(1, parseInt(limit, 10) || 12));
  const offset = (pageNum - 1) * limitNum;

  const conditions: string[] = ['p.active = 1'];
  const params: any[] = [];

  if (category) {
    conditions.push('c.slug = ?');
    params.push(category);
  }
  if (search) {
    conditions.push('(p.name LIKE ? OR p.description LIKE ? OR p.sku LIKE ?)');
    const q = `%${search}%`;
    params.push(q, q, q);
  }
  if (featured === 'true') {
    conditions.push('p.featured = 1');
  }

  const where = conditions.length ? 'WHERE ' + conditions.join(' AND ') : '';

  const total = (db.prepare(`
    SELECT COUNT(*) as c FROM products p
    JOIN categories c ON c.id = p.category_id
    LEFT JOIN inventory i ON i.product_id = p.id
    ${where}
  `).get(...params) as any).c;

  const products = db.prepare(`
    SELECT p.*, c.name as category_name, c.slug as category_slug,
           i.quantity, i.low_stock_threshold
    FROM products p
    JOIN categories c ON c.id = p.category_id
    LEFT JOIN inventory i ON i.product_id = p.id
    ${where}
    ORDER BY p.featured DESC, p.id ASC
    LIMIT ? OFFSET ?
  `).all(...params, limitNum, offset) as any[];

  return res.json({
    products,
    pagination: { page: pageNum, limit: limitNum, total, totalPages: Math.ceil(total / limitNum) }
  });
});

// GET /products/:slug
router.get('/:slug', (req, res) => {
  const product = db.prepare(`
    SELECT p.*, c.name as category_name, c.slug as category_slug,
           i.quantity, i.low_stock_threshold
    FROM products p
    JOIN categories c ON c.id = p.category_id
    LEFT JOIN inventory i ON i.product_id = p.id
    WHERE p.slug = ? AND p.active = 1
  `).get(req.params.slug) as any;
  if (!product) return res.status(404).json({ error: 'Product not found' });
  return res.json({ product });
});

// POST /products (admin)
router.post('/', requireAdmin, (req, res) => {
  const { category_id, name, description = '', price, sku, image_url = '', specs_json, featured = false, active = true, initial_stock = 0, low_stock_threshold = 5 } = req.body || {};
  if (!category_id || !name || !price || !sku) {
    return res.status(400).json({ error: 'category_id, name, price, sku required' });
  }
  const slug = sku.toLowerCase().replace(/[^a-z0-9]+/g, '-');
  try {
    const r = db.prepare(
      'INSERT INTO products (category_id,name,slug,description,price,sku,image_url,specs_json,featured,active) VALUES (?,?,?,?,?,?,?,?,?,?)'
    ).run(category_id, name, slug, description, price, sku, image_url, specs_json || null, featured ? 1 : 0, active ? 1 : 0) as unknown as RunResult;
    db.prepare('INSERT INTO inventory (product_id,quantity,low_stock_threshold) VALUES (?,?,?)').run(r.lastInsertRowid, initial_stock, low_stock_threshold);
    const product = db.prepare('SELECT p.*,i.quantity,i.low_stock_threshold FROM products p LEFT JOIN inventory i ON i.product_id=p.id WHERE p.id=?').get(r.lastInsertRowid) as any;
    return res.status(201).json({ product });
  } catch (e: any) {
    if (e.message?.includes('UNIQUE')) return res.status(409).json({ error: 'SKU already exists' });
    throw e;
  }
});

// PUT /products/:id (admin)
router.put('/:id', requireAdmin, (req, res) => {
  const id = parseInt(req.params.id, 10);
  const existing = db.prepare('SELECT * FROM products WHERE id=?').get(id) as any;
  if (!existing) return res.status(404).json({ error: 'Product not found' });

  const {
    category_id = existing.category_id,
    name = existing.name,
    description = existing.description,
    price = existing.price,
    sku = existing.sku,
    image_url = existing.image_url,
    specs_json = existing.specs_json,
    featured = existing.featured,
    active = existing.active,
  } = req.body || {};

  const slug = sku.toLowerCase().replace(/[^a-z0-9]+/g, '-');
  db.prepare(
    'UPDATE products SET category_id=?,name=?,slug=?,description=?,price=?,sku=?,image_url=?,specs_json=?,featured=?,active=? WHERE id=?'
  ).run(category_id, name, slug, description, price, sku, image_url, specs_json || null, featured ? 1 : 0, active ? 1 : 0, id);

  const product = db.prepare('SELECT p.*,i.quantity,i.low_stock_threshold FROM products p LEFT JOIN inventory i ON i.product_id=p.id WHERE p.id=?').get(id) as any;
  return res.json({ product });
});

// DELETE /products/:id (admin - soft delete)
router.delete('/:id', requireAdmin, (req, res) => {
  const id = parseInt(req.params.id, 10);
  const r = db.prepare('UPDATE products SET active=0 WHERE id=?').run(id) as unknown as RunResult;
  if (r.changes === 0) return res.status(404).json({ error: 'Product not found' });
  return res.json({ ok: true });
});

export default router;
