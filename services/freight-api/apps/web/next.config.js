/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  basePath: '/demos/trucking-freight-factoring-and-banking',
  trailingSlash: true,
  images: { unoptimized: true },
};

module.exports = nextConfig;
