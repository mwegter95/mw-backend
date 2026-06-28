import { Router } from 'express';
import { db } from '../db/database';

const router = Router();

router.get('/', (_req, res) => {
  const cats = db.prepare('SELECT * FROM categories ORDER BY display_order ASC').all() as any[];
  return res.json({ categories: cats });
});

export default router;
