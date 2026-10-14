#include "board.hpp"
#include "search.hpp"
#include <atomic>
#include <iostream>
#include <stdexcept>

void require(bool condition, const char *message) {
    if (!condition)
        throw std::runtime_error(message);
}
int main() {
    try {
        chess::Board b;
        require(chess::perft(b, 0) == 1, "perft 0");
        require(chess::perft(b, 1) == 20, "perft 1");
        require(chess::perft(b, 2) == 400, "perft 2");
        require(chess::perft(b, 3) == 8902, "perft 3");
        require(chess::perft(b, 4) == 197281, "perft 4");
        require(b.set_fen("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
                "kiwipete FEN");
        require(chess::perft(b, 3) == 97862, "kiwipete perft");
        require(b.set_fen("8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1"), "endgame FEN");
        require(chess::perft(b, 4) == 43238, "endgame perft");
        b.set_fen(chess::START_FEN);
        auto before = b.fen();
        auto key = b.key();
        auto history = b.history;
        for (auto m : b.legal_moves()) {
            auto old = b.make(m);
            b.unmake(old);
            require(b.fen() == before && b.key() == key && b.history == history,
                    "undo restores state");
        }
        for (int i = 0; i < 2; ++i)
            for (const char *text : {"g1f3", "g8f6", "f3g1", "f6g8"}) {
                auto m = b.parse_move(text);
                require(static_cast<bool>(m), "repetition move");
                b.make(m);
            }
        require(b.repeated(), "threefold");
        require(b.set_fen("7k/8/8/8/8/8/8/K7 w - - 0 1") && b.insufficient_material(),
                "bare kings");
        require(b.set_fen("7k/8/5KQ1/8/8/8/8/8 w - - 0 1"), "mate FEN");
        chess::Search search;
        std::atomic_bool stop{false};
        auto m = search.run(b, {3, -1}, stop, [](const auto &) {});
        require(static_cast<bool>(m), "search best move");
        b.make(m);
        require(b.in_check() && b.legal_moves().size == 0, "finds mate in one");
        require(b.set_fen(chess::START_FEN), "reset");
        stop = true;
        require(static_cast<bool>(search.run(b, {64, 1000}, stop, [](const auto &) {})),
                "stop fallback");
        std::cout << "Core tests passed\n";
    } catch (const std::exception &e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
