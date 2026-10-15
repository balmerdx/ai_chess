#include "book.hpp"
#include <algorithm>
#include <charconv>
#include <cmath>
#include <fstream>
#include <iostream>
#include <sstream>

int main(int argc, char **argv) {
    if (argc < 3 || (argc - 3) % 2 != 0) {
        std::cerr << "Usage: chess_book own-games.txt output.book [--max-plies N] "
                     "[--min-games N] [--score-margin F]\n";
        return 2;
    }
    int max_plies = 20, min_games = 1;
    double score_margin = 1;
    for (int i = 3; i < argc; i += 2) {
        const std::string option = argv[i], text = argv[i + 1];
        if (option == "--score-margin") {
            auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), score_margin);
            if (error != std::errc{} || end != text.data() + text.size() ||
                !std::isfinite(score_margin) || score_margin < 0 || score_margin > 1) {
                std::cerr << "Invalid score margin\n";
                return 2;
            }
        } else if (option == "--max-plies" || option == "--min-games") {
            int number = 0;
            auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), number);
            if (error != std::errc{} || end != text.data() + text.size() || number < 1 ||
                (option == "--max-plies" && number > 20)) {
                std::cerr << "Invalid count\n";
                return 2;
            }
            (option == "--max-plies" ? max_plies : min_games) = number;
        } else {
            std::cerr << "Unknown option: " << option << '\n';
            return 2;
        }
    }
    std::ifstream in(argv[1]);
    std::string line;
    if (!std::getline(in, line) || (line != "AI_CHESS_OWN_GAMES 1" && line != "AI_CHESS_OWN_GAMES 2")) {
        std::cerr << "Invalid own-games export\n";
        return 2;
    }
    const bool candidate_only = line == "AI_CHESS_OWN_GAMES 2";
    chess::Book book;
    int games = 0;
    while (std::getline(in, line)) {
        std::istringstream row(line);
        std::string result, token;
        if (!(row >> result) || (result != "1-0" && result != "0-1" && result != "1/2-1/2")) {
            std::cerr << "Invalid result at game " << games + 1 << '\n';
            return 2;
        }
        std::string candidate_color;
        if (candidate_only && (!(row >> candidate_color) ||
                               (candidate_color != "white" && candidate_color != "black"))) {
            std::cerr << "Invalid candidate color at game " << games + 1 << '\n';
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
            if (ply++ < max_plies)
                opening.emplace_back(b.key(), m);
            b.make(m);
        }
        for (std::size_t i = 0; i < opening.size(); ++i) {
            if (candidate_only && ((i % 2 == 0) != (candidate_color == "white")))
                continue;
            int outcome = result == "1/2-1/2" ? 0 : ((result == "1-0") == (i % 2 == 0) ? 1 : -1);
            book.add(opening[i].first, opening[i].second, outcome);
        }
        ++games;
    }
    auto count = [](const auto &r) {
        return static_cast<double>(r.wins) + static_cast<double>(r.draws) +
               static_cast<double>(r.losses);
    };
    auto score = [&](const auto &r) {
        return (static_cast<double>(r.wins) + .5 * static_cast<double>(r.draws) + 1) /
               (count(r) + 2);
    };
    for (auto it = book.entries.begin(); it != book.entries.end();) {
        auto &records = it->second;
        std::erase_if(records, [&](const auto &r) { return count(r) < min_games; });
        double best = 0;
        for (const auto &r : records)
            best = std::max(best, score(r));
        std::erase_if(records, [&](const auto &r) { return score(r) + score_margin + 1e-12 < best; });
        if (records.empty())
            it = book.entries.erase(it);
        else
            ++it;
    }
    if (!in.eof() || !book.save(argv[2])) {
        std::cerr << "I/O failure\n";
        return 1;
    }
    std::cout << "Built book from " << games << " games, " << book.entries.size() << " positions\n";
}
