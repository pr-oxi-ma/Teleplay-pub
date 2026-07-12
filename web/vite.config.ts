import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
    const env = loadEnv(mode, '.', '')

    const devServerPort = Number(env.VITE_DEV_SERVER_PORT || '5000')
    const allowedHosts = (env.VITE_ALLOWED_HOSTS || 'localhost')
        .split(',')
        .map((host) => host.trim())
        .filter(Boolean)
    const proxyTarget = (env.VITE_API_BASE_URL || 'http://localhost:5001')
        .replace(/\/+$/, '')
        .replace(/\/api$/, '')

    return {
        plugins: [react()],
        server: {
            port: devServerPort,
            host: true,
            allowedHosts,
            proxy: {
                '/api': {
                    target: proxyTarget,
                    changeOrigin: true,
                },
            },
        },
    }
})
