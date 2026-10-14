#include "book.hpp"
#include "search.hpp"
#include <algorithm>
#include <atomic>
#include <charconv>
#include <iostream>
#include <mutex>
#include <sstream>
#include <thread>

namespace {
std::mutex output_mutex;
void output(const std::string &text) {
    std::lock_guard lock(output_mutex);
    std::cout << text << '\n' << std::flush;
}
int number(const std::string &text, int min, int max) {
    int n;
    auto [end, ec] = std::from_chars(text.data(), text.data() + text.size(), n);
    if (ec != std::errc{} || end != text.data() + text.size() || n < min || n > max)
        throw std::runtime_error("Invalid integer: " + text);
    return n;
}
bool position(chess::Board &board, std::istringstream &in, std::string &error) {
    std::string token, fen;
    in >> token;
    chess::Board candidate;
    if (token == "startpos")
        fen = chess::START_FEN;
    else if (token == "fen") {
        for (int i = 0; i < 6; ++i) {
            if (!(in >> token)) {
                error = "Missing FEN fields";
                return false;
            }
            if (i)
                fen += ' ';
            fen += token;
        }
    } else {
        error = "Expected startpos or fen";
        return false;
    }
    if (!candidate.set_fen(fen, &error))
        return false;
    if (in >> token) {
        if (token != "moves") {
            error = "Expected moves";
            return false;
        }
        while (in >> token) {
            auto m = candidate.parse_move(token);
            if (!m) {
                error = "Illegal move: " + token;
                return false;
            }
            candidate.make(m);
        }
    }
    board = std::move(candidate);
    return true;
}
} // namespace
int main(int argc, char **argv) {
    try {
        if (argc > 1) {
            chess::Board b;
            std::string error;
            std::string command = argv[1];
            if (command == "--perft" && argc >= 3) {
                int depth = number(argv[2], 0, 8);
                if (argc >= 4 && !b.set_fen(argv[3], &error))
                    throw std::runtime_error(error);
                std::cout << chess::perft(b, depth) << '\n';
                return 0;
            }
            if (command == "--inspect" && argc >= 3) {
                if (!b.set_fen(argv[2], &error))
                    throw std::runtime_error(error);
                for (int i = 3; i < argc; ++i) {
                    auto m = b.parse_move(argv[i]);
                    if (!m)
                        throw std::runtime_error("Illegal move");
                    b.make(m);
                }
                std::cout << b.fen() << '\n' << b.key() << '\n' << (b.draw() ? 1 : 0) << '\n';
                for (auto m : b.legal_moves())
                    std::cout << m.uci() << ' ';
                std::cout << '\n';
                return 0;
            }
            throw std::runtime_error(
                "Usage: ai_chess_engine [--perft depth [FEN] | --inspect FEN [moves...]]");
        }
        chess::Board board;
        chess::Search search;
        chess::Book book;
        bool own_book = false;
        std::mt19937_64 rng(1);
        std::atomic_bool stop{false};
        std::thread worker;
        auto finish = [&]() {
            stop.store(true);
            if (worker.joinable())
                worker.join();
        };
        std::string line;
        while (std::getline(std::cin, line)) {
            std::istringstream in(line);
            std::string command;
            in >> command;
            try {
                if (command == "uci")
                    output("id name ai_chess 0.1\nid author ai_chess\noption name Hash type spin "
                           "default 16 min 1 max 256\noption name OwnBook type check default "
                           "false\noption name BookFile type string default <empty>\noption name "
                           "Seed type spin default 1 min 0 max 2147483647\nuciok");
                else if (command == "isready")
                    output("readyok");
                else if (command == "stop")
                    finish();
                else if (command == "quit") {
                    finish();
                    break;
                } else if (command == "ucinewgame") {
                    finish();
                    search.clear();
                } else if (command == "position") {
                    finish();
                    std::string error;
                    if (!position(board, in, error))
                        output("info string " + error);
                } else if (command == "setoption") {
                    finish();
                    std::string marker, name, value, token;
                    in >> marker;
                    if (marker != "name")
                        throw std::runtime_error("Expected option name");
                    while (in >> token && token != "value") {
                        if (!name.empty())
                            name += ' ';
                        name += token;
                    }
                    if (token == "value") {
                        std::getline(in, value);
                        if (!value.empty() && value[0] == ' ')
                            value.erase(0, 1);
                    }
                    if (name == "Hash")
                        search.resize(number(value, 1, 256));
                    else if (name == "OwnBook") {
                        if (value != "true" && value != "false")
                            throw std::runtime_error("Expected true or false");
                        own_book = value == "true";
                    } else if (name == "Seed")
                        rng.seed(number(value, 0, 2147483647));
                    else if (name == "BookFile") {
                        if (value.empty() || value == "<empty>")
                            book.entries.clear();
                        else {
                            std::string error;
                            if (!book.load(value, error)) {
                                book.entries.clear();
                                throw std::runtime_error(error);
                            }
                        }
                    }
                } else if (command == "go") {
                    finish();
                    chess::Limits limits;
                    limits.milliseconds = -1;
                    bool has_limit = false;
                    std::string token;
                    while (in >> token) {
                        if (token == "depth") {
                            in >> token;
                            limits.depth = number(token, 1, 64);
                            has_limit = true;
                        } else if (token == "movetime") {
                            in >> token;
                            limits.milliseconds = number(token, 0, 86400000);
                            has_limit = true;
                        } else if (token == "infinite")
                            has_limit = true;
                    }
                    if (!has_limit)
                        limits.milliseconds = 1000;
                    stop.store(false);
                    worker = std::thread([&, copy = board, limits]() mutable {
                        try {
                            auto best = own_book ? book.probe(copy, rng) : chess::Move{};
                            if (best)
                                output("info string book move");
                            else
                                best = search.run(
                                    std::move(copy), limits, stop,
                                    [](const chess::SearchInfo &info) {
                                        std::ostringstream out;
                                        out << "info depth " << info.depth << " score ";
                                        if (std::abs(info.score) > chess::MATE - 128) {
                                            int plies = chess::MATE - std::abs(info.score);
                                            out << "mate "
                                                << (info.score >= 0 ? 1 : -1) * ((plies + 1) / 2);
                                        } else
                                            out << "cp " << info.score;
                                        out << " nodes " << info.nodes << " time "
                                            << info.milliseconds << " nps "
                                            << info.nodes * 1000 /
                                                   std::max<long long>(1, info.milliseconds)
                                            << " pv";
                                        for (auto m : info.pv)
                                            out << ' ' << m.uci();
                                        output(out.str());
                                    });
                            output("bestmove " + best.uci());
                        } catch (const std::exception &e) {
                            std::cerr << e.what() << '\n';
                            output("bestmove 0000");
                        }
                    });
                }
            } catch (const std::exception &e) {
                output(std::string("info string ") + e.what());
            }
        }
        finish();
    } catch (const std::exception &e) {
        std::cerr << e.what() << '\n';
        return 2;
    }
}
