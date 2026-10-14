#include "book.hpp"
#include <fstream>
#include <iostream>
#include <sstream>

int main(int argc, char **argv) {
    if (argc != 3) {
        std::cerr << "Usage: chess_book own-games.txt output.book\n";
        return 2;
    }
    std::ifstream in(argv[1]);
    std::string line;
    if (!std::getline(in, line) || line != "AI_CHESS_OWN_GAMES 1") {
        std::cerr << "Invalid own-games export\n";
        return 2;
    }
    chess::Book book;
    int games = 0;
    while (std::getline(in, line)) {
        std::istringstream row(line);
        std::string result, token;
        if (!(row >> result) || (result != "1-0" && result != "0-1" && result != "1/2-1/2")) {
            std::cerr << "Invalid result at game " << games + 1 << '\n';
            return 2;
        }
        chess::Board b;
        std::vector<std::pair<chess::Bits, chess::Move>> opening;
        int ply = 0;
        while (row >> token) {
            auto m = b.parse_move(token);
            if (!m || b.draw()) {
                std::cerr << "Invalid move at game " << games + 1 << '\n';
                return 2;
            }
            if (ply++ < 20)
                opening.emplace_back(b.key(), m);
            b.make(m);
        }
        for (std::size_t i = 0; i < opening.size(); ++i) {
            int outcome = result == "1/2-1/2" ? 0 : ((result == "1-0") == (i % 2 == 0) ? 1 : -1);
            book.add(opening[i].first, opening[i].second, outcome);
        }
        ++games;
    }
    if (!in.eof() || !book.save(argv[2])) {
        std::cerr << "I/O failure\n";
        return 1;
    }
    std::cout << "Built book from " << games << " games, " << book.entries.size() << " positions\n";
}
