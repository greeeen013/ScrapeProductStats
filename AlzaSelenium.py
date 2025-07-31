from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
import undetected_chromedriver as uc
import time
import logging

# Nastavení logování
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(levelname)s] %(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

def alza_wait_and_close():
    # Nastavení Chrome options
    chrome_options = Options()
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.5735.199 Safari/537.36"
    )
    #chrome_options.add_argument("--headless=new")  # Pro běh na pozadí
    chrome_options.add_argument("--window-size=1920,1080")

    driver = None
    try:
        # Inicializace prohlížeče
        driver = uc.Chrome(options=chrome_options)
        logger.debug("Browser initialized")

        # Načtení stránky Alza.cz
        url = "https://www.alza.cz"
        logger.debug(f"Navigating to {url}")
        driver.get(url)

        # Počkáme 30 sekund
        logger.debug("Waiting for 30 seconds...")
        time.sleep(30)

        # Zde bude později další kód
        logger.debug("30 seconds passed, ready for next steps")

    except Exception as e:
        logger.error(f"Error in alza_wait_and_close: {str(e)}", exc_info=True)
    finally:
        # Ukončení prohlížeče
        if driver:
            try:
                driver.quit()
                logger.debug("Browser closed")
            except Exception as e:
                logger.error(f"Error closing browser: {str(e)}")

# Testovací spuštění
if __name__ == "__main__":
    alza_wait_and_close()