"""Virtual paper trading. Nothing here talks to a broker, a market-data provider or the network, and nothing can place a real order:
the engine only reads a MarketView handed to it and keeps its own virtual cash, positions and ledger in memory.
"""
