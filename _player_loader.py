import sys
sys.path.insert(0, '/opt/radxa_data/teslausb')
# The Flask app object is defined in __main__ when app.py runs directly
import __main__
app = getattr(__main__, 'app', None)
if app is not None:
    try:
        from player_routes import register_player_routes
        register_player_routes(app)
        print("Player routes registered successfully")
    except Exception as e:
        import logging
        logging.warning(f"Player routes load failed: {e}")
else:
    import logging
    logging.warning("Cannot find Flask app object for player routes")
