import asyncio
import uvicorn
import sys
import os

async def start_server():
    from app.main import app
    
    # Grab Render's dynamic port, or use 5001 if running locally
    port = int(os.environ.get("PORT", 5001))
    
    config = uvicorn.Config(
        app=app, 
        host="0.0.0.0", 
        port=port,
        loop="asyncio"
    )
    
    while True:
        try:
            print(f"\n🔄 [TelePlay] Starting Backend Server on port {port}...")
            server = uvicorn.Server(config)
            await server.serve()
            
            print("⚠️ [TelePlay] Server stopped unexpectedly. Restarting in 3 seconds...")
            
        except Exception as e:
            print(f"💥 [TelePlay] Backend CRASHED with error: {e}", file=sys.stderr)
            print("🔄 [TelePlay] Attempting auto-restart in 3 seconds...")
        
        await asyncio.sleep(3)

if __name__ == "__main__":
    try:
        asyncio.run(start_server())
    except KeyboardInterrupt:
        print("\n🛑 [TelePlay] Server stopped manually by user (Ctrl+C). Exiting...")