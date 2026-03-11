async def run_bot():
    logger.info("🔴 run_bot: INIZIO")
    bot = PaviaBot()
    try:
        logger.info("🔴 run_bot: prima di bot.start()")
        await bot.start(Config.DISCORD_TOKEN)
        logger.info("🔴 run_bot: dopo bot.start()")
        
        await bot.wait_until_ready()
        logger.info("🔴 run_bot: dopo wait_until_ready()")
        
        logger.info(f"Logged in as {bot.user}")
        
        # DEBUG ULTIMATIVO - Controlla se activity_monitor esiste
        logger.info(f"🔍 activity_monitor exists: {bot.activity_monitor is not None}")
        if bot.activity_monitor:
            logger.info(f"🔍 daily_check task exists: {hasattr(bot.activity_monitor, 'daily_check')}")
            logger.info(f"🔍 daily_check is running before start: {bot.activity_monitor.daily_check.is_running() if hasattr(bot.activity_monitor, 'daily_check') else 'N/A'}")
        else:
            logger.error("❌ activity_monitor is None! Check setup_hook")
        
        # Avvio dei task con gestione errori
        try:
            logger.info("🔴 run_bot: prima di daily_backup.start()")
            bot.daily_backup.start()
            logger.info("✅ daily_backup started")
            
            logger.info("🔴 run_bot: prima di daily_check.start()")
            if bot.activity_monitor and hasattr(bot.activity_monitor, 'daily_check'):
                bot.activity_monitor.daily_check.start()
                logger.info(f"🟢 daily_check started: {bot.activity_monitor.daily_check.is_running()}")
            else:
                logger.error("❌ Cannot start daily_check: activity_monitor or daily_check missing")
        except Exception as e:
            logger.error(f"❌ Failed to start daily_check: {e}")
            import traceback
            traceback.print_exc()
        
        logger.info("🔴 run_bot: prima di print e Future")
        print(f"✅ Bot online as {bot.user}")
        await asyncio.Future()  # run forever
        logger.info("🔴 run_bot: DOPO Future (non dovrebbe mai arrivare)")
        
    except Exception as e:
        logger.error(f"Fatal error during bot.run: {e}")
        import traceback
        traceback.print_exc()
        await bot.close()
        raise
    finally:
        logger.info("🔴 run_bot: FINALLY")
        await bot.close()
